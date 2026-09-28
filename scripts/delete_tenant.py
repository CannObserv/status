"""Delete a tenant, everything it owns, and leave a record of what went.

The one operation this service had no sanctioned path for. ``seed_tenant.py``
creates a tenant and ``rotate_key.py`` attaches, revokes and rotates its keys;
neither deletes one. So a tenant delete was ad-hoc SQL or an ad-hoc ORM
program against production — the precise failure notifier#62 was filed out of, and
``rotate_key.py`` was written to retire, still live for the *larger* operation
(notifier#79).

Larger, and quieter. A revoke retires one credential; this retires every
credential the consumer holds. And done by hand it leaves nothing behind: the
keys go by ``ON DELETE CASCADE``, never through ``revoke()``, so none of
notifier#67's audit records would be emitted and the rows would be gone.

Usage (dev database — served by status-dev.service on :9001)::

    . scripts/load_env.sh

    # Read first. A rehearsal names everything the real run would destroy.
    DATABASE_URL="$DEV_DATABASE_URL" \\
        uv run python scripts/delete_tenant.py --tenant-id <id> --dry-run

    # Then do it, naming the tenant you believe you are deleting
    DATABASE_URL="$DEV_DATABASE_URL" \\
        uv run python scripts/delete_tenant.py --tenant-id <id> \\
            --expect-name <name>

Usage (production — deliberate, opt-in)::

    . scripts/load_env.sh
    STATUS_ALLOW_PROD_DB=1 uv run python scripts/delete_tenant.py …

The connection is opened through ``src.core.database``, so the guard in
``src/core/db_safety.py`` applies unchanged: production requires the opt-in
above, on the command line, for the single invocation — never in an env file.

Three things this does that a hand-written ``DELETE FROM tenants`` could not:

* **Names the victim before it goes.** Every key id, label, prefix and
  ``last_used_at``.
* **``--dry-run``.** Rehearses the whole operation, refusals included, and
  rolls back.
* **Leaves a record.** One ``status.audit`` record per cascaded key plus a
  closing summary — ``journalctl -t status-keys`` — naming each key and
  never its secret, so the channel stays the one answer to "which credential
  died, and when".

``--expect-name`` is this script's own guard, and is **required with
``--yes``**. A revoke names one key on one tenant, and a mistyped ULID there
is caught by the ownership check. Here the ULID *is* the whole target: a typo
that happens to name another consumer deletes that consumer instead, and the
prompt — which prints the name — is the only other place a human reads it.
``--yes`` removes the prompt, so it must supply the name in its place.
"""

import argparse
import asyncio
import sys
from datetime import datetime

from src.core.database import get_session_factory
from src.core.logging import AUDIT_IDENT, configure_script_logging
from src.core.tenants import TenantInventory, delete_tenant, inventory_of
from src.core.utils import format_utc_iso

#: Exit codes, spelled the same as ``rotate_key.py`` so a caller reading both
#: reads one vocabulary. ``1`` is that script's "committed but not proven";
#: nothing here verifies anything against a live endpoint — a deleted tenant
#: has no key left to present — so it is left unused rather than reassigned.
OK = 0
REFUSED = 2
ABORTED = 3


class NameMismatchError(LookupError):
    """Raised when ``--expect-name`` does not name the tenant being deleted."""


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser. Split out so tests can read its help."""
    parser = argparse.ArgumentParser(
        prog="delete_tenant.py",
        description=("Delete a tenant and everything it owns, recording every key that goes."),
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help=(
            "the tenant to destroy. Its id, never its name — a name is a label an operator renames"
        ),
    )
    parser.add_argument(
        "--expect-name",
        help=(
            "the name you believe that id belongs to; the run refuses if it does not match. "
            "Required with --yes"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="rehearse the whole operation, refusals included, and roll back",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt. Required when stdin is not a terminal",
    )
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse *argv*, rejecting combinations that cannot mean anything."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.yes and args.expect_name is None:
        parser.error(
            "--yes skips the prompt, which is the only place the tenant's name is read back. "
            "Pass --expect-name <name> so the id is checked against something"
        )
    # `--dry-run --yes` is deliberately allowed. A rehearsal is worth most
    # when it is the real command line plus one flag — refusing the pair made
    # the rehearsal of an unattended run impossible to spell, and diverged
    # from `rotate_key.py`, which accepts both (CR 2). `--yes` simply has
    # nothing to skip on a run that never reaches the prompt, and
    # `--expect-name` is still required alongside it, so the rehearsal fires
    # the same name check the real run will.
    return args


def _timestamp(value: datetime | None) -> str:
    """Render a key's timestamp the way the rest of the service renders one."""
    return "never" if value is None else format_utc_iso(value)


def render(inventory: TenantInventory, dry_run: bool = False) -> list[str]:
    """Return the lines to print for *inventory*.

    Never prints ``key_hash``: the prefix is what identifies a key to a human,
    the digest is what verifies it, and this output is likelier than most to
    be pasted into an incident channel.

    The keys are listed individually: a key is a credential someone may still
    be holding and may have to be told about.
    """
    lines: list[str] = []
    if dry_run:
        lines.append("DRY RUN — nothing was written")
    lines.append(f"tenant_id={inventory.tenant_id}")
    lines.append(f"tenant_name={inventory.tenant_name}")

    if inventory.keys:
        lines.append(f"keys destroyed ({len(inventory.keys)}), oldest first:")
        for key in inventory.keys:
            lines.append(f"  key_id={key.id}")
            lines.append(f"    label={key.label}")
            lines.append(f"    key_prefix={key.key_prefix}")
            lines.append(f"    environment={key.environment}")
            lines.append(f"    created_at={_timestamp(key.created_at)}")
            lines.append(f"    last_used_at={_timestamp(key.last_used_at)}")
    else:
        lines.append("keys destroyed (0) — this tenant held none")
    return lines


def _describe(inventory: TenantInventory) -> str:
    """One line naming exactly what is about to happen, for the prompt."""
    return (
        f"About to permanently delete tenant {inventory.tenant_id} "
        f"({inventory.tenant_name}) and its {len(inventory.keys)} key(s). "
        "This cannot be undone."
    )


def _confirm(inventory: TenantInventory, yes: bool) -> bool:
    """Ask before writing. ``--yes`` skips; a non-terminal stdin must pass it.

    The prompt goes to stderr with the description, not through ``input``'s
    own argument, which writes to stdout. An operator piping stdout — ``| tee
    deletion.txt`` is the natural thing to do for a run you want a copy of —
    would otherwise see the warning and then nothing, while the script sat
    waiting on a prompt that had gone into the pipe. An invisible prompt is a
    bad place to split the stream in general, and the worst one in front of a
    delete that cannot be undone (CR 10).

    Backing out is an abort, not a crash. Ctrl-D and Ctrl-C both raise out of
    ``input()``, and an uncaught one prints a stack trace where a refusal
    belongs — which reads like the script broke part-way through rather than
    like it declined, the ambiguity this script refuses to create anywhere
    else. Both land on the caller's "Aborted; nothing was written." (CR 14).

    ``rotate_key.py:_confirm`` is this function's sibling and shares the whole
    contract above — describe on stderr, prompt on stderr, refuse an
    unattended stdin, treat backing out as an abort. The CR 10 defect existed
    in both because one was copied from the other; carry the rules across, not
    just the code (CR 15).
    """
    if yes:
        return True
    print(_describe(inventory), file=sys.stderr)
    if not sys.stdin.isatty():
        print(
            "Refusing to act unattended: stdin is not a terminal and --yes was not passed.",
            file=sys.stderr,
        )
        return False
    print(f"Type the tenant's name ({inventory.tenant_name}) to proceed: ", end="", file=sys.stderr)
    sys.stderr.flush()
    try:
        typed = input()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)  # the ^C or ^D sits at the end of the prompt line
        return False
    return typed.strip() == inventory.tenant_name


def _check_name(inventory: TenantInventory, expected: str | None) -> None:
    """Refuse when *expected* names a different tenant than the id does."""
    if expected is not None and expected != inventory.tenant_name:
        raise NameMismatchError(
            f"tenant {inventory.tenant_id} is named {inventory.tenant_name!r}, "
            f"not {expected!r} — check the id before deleting anything"
        )


async def main(args: argparse.Namespace) -> int:
    """Run the deletion and print the result. Returns an exit code."""
    try:
        factory = get_session_factory()
    except RuntimeError as exc:  # ProductionDatabaseError is one of these
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED

    # Read before deciding: the prompt has to name what is about to go, and a
    # refusal has to fire before anything is written. The session is opened
    # and closed around the read so the operator's thinking time is not spent
    # holding a transaction open against production.
    try:
        async with factory() as session:
            inventory = await inventory_of(session, args.tenant_id)
            _check_name(inventory, args.expect_name)
    except (LookupError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return REFUSED

    if not args.dry_run and not _confirm(inventory, args.yes):
        print("Aborted; nothing was written.", file=sys.stderr)
        return ABORTED

    try:
        async with factory() as session:
            # Re-read inside the writing transaction. The inventory printed
            # above is a moment older than the delete, and it is the delete's
            # own snapshot that the audit records are built from — a key
            # minted between the two would otherwise be destroyed with no
            # record naming it.
            inventory = await delete_tenant(session, args.tenant_id, dry_run=args.dry_run)
    except (LookupError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return REFUSED

    for line in render(inventory, dry_run=args.dry_run):
        print(line)
    if args.dry_run:
        print("Re-run without --dry-run, passing --expect-name, to perform it.")
    else:
        # Otherwise a real run's output is the rehearsal's minus one header
        # line, and the operator is left to know the channel from memory —
        # for the record that is the entire point of this script (CR 9).
        print(f"Recorded on the credential audit channel: journalctl -t {AUDIT_IDENT}")
    return OK


if __name__ == "__main__":
    # Parse first: a rejected flag combination exits here, before the audit
    # channel is opened for an operation that is not going to happen.
    _args = parse_args(sys.argv[1:])
    configure_script_logging()
    raise SystemExit(asyncio.run(main(_args)))
