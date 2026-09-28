"""Attach, revoke, and rotate API keys on a tenant that already exists.

``seed_tenant.py`` always creates a new tenant, so until notifier#62 there was no
sanctioned way to give an existing consumer a second credential, and none at
all to rotate one. The rotation that forced the issue — a key exposed during
notifier#57 — went through a program written against the ORM while the credential was
live. It was reviewed as it was written and it was correct. The defect is that
it had to be composed under time pressure at all: the next person, in the same
hurry, reaches for ``INSERT INTO api_keys …`` instead, and does it to
production with no dry run, no confirmation of which row they just deleted,
and no record.

Three operations, one script::

    # attach a second key to an existing tenant
    rotate_key.py --tenant-id <id> --new-label backup

    # revoke one, naming it explicitly
    rotate_key.py --tenant-id <id> --revoke <key-id>

    # rotate: both halves, one transaction
    rotate_key.py --tenant-id <id> --new-label new --revoke <old-key-id>

Usage (dev database — served by status-dev.service on :9001)::

    . scripts/load_env.sh
    DATABASE_URL="$DEV_DATABASE_URL" uv run python scripts/rotate_key.py …

Usage (production — deliberate, opt-in)::

    . scripts/load_env.sh
    STATUS_ALLOW_PROD_DB=1 uv run python scripts/rotate_key.py …

The connection is opened through ``src.core.database``, so the guard in
``src/core/db_safety.py`` applies unchanged: production requires the opt-in
above, on the command line, for the single invocation — never in an env file.

Five things this does that the ad-hoc program could not:

* **One transaction.** The mint and the revoke commit together, so an
  interrupted rotation cannot leave a tenant holding a key nobody has.
* **Names the victim.** Revocation takes a key id, never "the other one",
  which silently does the wrong thing to a tenant holding three. The key's
  prefix, label and ``last_used_at`` are printed before it goes — the last of
  those being the field that says whether something is actively using it.
* **``--dry-run``.** Rehearses the whole operation, refusals included, and
  rolls back.
* **``--verify``.** Proves the new key authenticates against the real
  endpoint. A rotation nobody verified is a rotation that might have revoked
  the wrong row.
* **Leaves a record.** Both halves land on the audit channel —
  ``journalctl -t status-keys`` — naming the key and never its secret. notifier#62
  declined a ``revoked_at`` column on the strength of that line, and until notifier#67
  the line was being dropped before it reached any stream: a revoke left the
  row deleted and nothing at all to say which one, or when.

Verification is deliberately asymmetric. The script always holds the raw key
it just minted, so it can always prove that one works. It only ever held the
*hash* of the key it revoked, so proving the old one is dead needs the
operator to supply it — which they have in an exposure incident, and do not
in a routine rotation. ``--verify-old`` is that half, and its absence is
reported rather than passed over.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.api_keys import (
    ENVIRONMENTS,
    KeyRecord,
    LastKeyError,
    key_count,
    keys_for,
    mint,
    revoke,
    ulid_str,
)
from src.core.database import get_session_factory
from src.core.logging import configure_script_logging
from src.core.utils import format_utc_iso

#: An authenticated endpoint that costs a tenant with no data nothing to
#: serve. Its job is to exercise ``require_api_key``, not to return rows.
#: Any authenticated GET works; the monitor list is the cheapest one co-status has.
VERIFY_PATH = "/api/v1/monitors"

#: How long a verification request may take before it is a failed check.
VERIFY_TIMEOUT_SECONDS = 10.0

#: Applied after argument parsing, not as argparse's ``default``: the guard
#: that refuses ``--environment`` without ``--new-label`` needs to be able to
#: tell "not passed" from "passed the default value" (CR 1).
DEFAULT_ENVIRONMENT = "production"

#: Exit codes, distinguished so a caller can tell the three apart. A refusal
#: means the database is untouched; a failed verification means it is not.
OK = 0
NOT_VERIFIED = 1
REFUSED = 2
ABORTED = 3


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a run did, or would have done under ``--dry-run``."""

    tenant_id: str
    minted: KeyRecord | None
    raw_key: str | None
    revoked: KeyRecord | None
    dry_run: bool
    remaining_keys: int


@dataclass(frozen=True, slots=True)
class Check:
    """One post-rotation probe against the live endpoint."""

    name: str
    ok: bool
    detail: str


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser. Split out so tests can read its help."""
    parser = argparse.ArgumentParser(
        prog="rotate_key.py",
        description="Attach, revoke, or rotate an API key on an existing tenant.",
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="the tenant to act on. Its id, never its name — a name is a label an operator renames",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print this tenant's keys and exit. Read-only; the id you need to --revoke is here",
    )
    parser.add_argument("--new-label", help="mint a key with this label")
    parser.add_argument(
        "--environment",
        default=None,
        choices=ENVIRONMENTS,
        help="which deployment the new key is for (default: production)",
    )
    parser.add_argument("--revoke", metavar="KEY_ID", help="delete this key, named explicitly")
    parser.add_argument(
        "--force",
        action="store_true",
        help="permit revoking a tenant's last key, leaving that consumer unable to authenticate",
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
    parser.add_argument(
        "--verify",
        metavar="BASE_URL",
        help=f"after committing, GET {VERIFY_PATH} there and check the new key is accepted",
    )
    parser.add_argument(
        "--verify-old",
        metavar="RAW_KEY",
        help=(
            "the raw key being revoked, so its 401 can be proven too. The script never held it, "
            "only its hash"
        ),
    )
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse *argv*, rejecting flag combinations that cannot mean anything.

    Every refusal here is a flag that would otherwise be silently ignored, and
    a silently ignored flag in an incident tool is an operator believing they
    asked for something they did not get.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Every one of these is a flag that would otherwise be accepted and then
    # do nothing. `--environment` is read off the parsed value rather than off
    # argv: `"--environment" in argv` matched only the space-separated
    # spelling, so `--environment=development` sailed past this guard and was
    # silently ignored (CR 1).
    others = (
        args.new_label,
        args.revoke,
        args.environment,
        args.force or None,
        args.dry_run or None,
        args.yes or None,
        args.verify,
        args.verify_old,
    )
    if args.list and any(other is not None for other in others):
        parser.error("--list is the read you do before deciding; run it on its own")
    if not args.list and args.new_label is None and args.revoke is None:
        parser.error("nothing to do: pass --list, --new-label, --revoke, or both to rotate")
    if args.new_label is None and args.environment is not None:
        parser.error(
            "--environment applies to a key being minted; it does not retag an existing one"
        )
    if args.force and args.revoke is None:
        parser.error("--force permits revoking a tenant's last key; nothing is being revoked")
    if args.verify_old is not None and args.revoke is None:
        parser.error("--verify-old proves a revoked key is dead; nothing is being revoked")
    if args.verify_old is not None and args.verify is None:
        parser.error("--verify-old needs --verify: there is nowhere to send the request")
    if args.environment is None:
        args.environment = DEFAULT_ENVIRONMENT
    return args


async def apply(
    session: AsyncSession,
    *,
    tenant_id: str,
    new_label: str | None = None,
    environment: str = DEFAULT_ENVIRONMENT,
    revoke_id: str | None = None,
    force: bool = False,
    dry_run: bool = True,
) -> Outcome:
    """Perform the requested operations in one transaction; return what happened.

    Order matters. The mint goes first and flushes, so by the time the revoke
    counts the tenant's keys the replacement is already there — which is why
    the last-key guard never fires on a rotation, and why ``--force`` is not
    reachable on that path.

    ``dry_run`` rolls back instead of committing, and is the default here so a
    caller that forgets to pass it writes nothing. Every refusal still fires:
    a dry run that passes where the real run would fail is a rehearsal that
    certifies the wrong thing.
    """
    tenant_id = ulid_str(tenant_id)
    minted: KeyRecord | None = None
    raw: str | None = None
    revoked: KeyRecord | None = None

    try:
        if new_label is not None:
            key, raw = await mint(session, tenant_id, new_label, environment)
            minted = KeyRecord.of(key)
        if revoke_id is not None:
            revoked = await revoke(session, tenant_id, revoke_id, allow_last=force)
        remaining = await key_count(session, tenant_id)
    except Exception:
        await session.rollback()
        raise

    if dry_run:
        await session.rollback()
        raw = None
    else:
        await session.commit()

    return Outcome(
        tenant_id=tenant_id,
        minted=minted,
        raw_key=raw,
        revoked=revoked,
        dry_run=dry_run,
        remaining_keys=remaining,
    )


def _timestamp(value: datetime | None) -> str:
    """Render a key's timestamp the way the rest of the service renders one.

    AGENTS.md fixes ISO 8601 with a ``Z`` suffix, and ``format_utc_iso`` is
    what the rest of ``src/`` uses; these lines were printing a
    raw ``datetime``, a shape nothing else here emits (CR 11). ``None`` reads
    as ``never`` rather than as Python leaking into a terminal — for a key
    about to be revoked, "never used" is the fact the operator wants.
    """
    return "never" if value is None else format_utc_iso(value)


def render(outcome: Outcome) -> list[str]:
    """Return the lines to print for *outcome*.

    ``tenant_id=`` and ``raw_key=`` are flush left, keeping the shape
    ``seed_tenant.py`` prints so an operator's eye lands in the same place and
    an existing habit of copying one line still works. Everything else is
    indented under a ``revoked:`` or ``minted:`` heading, because a run can
    carry one of each and two flat ``environment=`` lines would not say which
    key either belonged to. The raw key appears exactly once and nowhere else
    — not in a log, not in a summary.
    """
    lines: list[str] = []
    if outcome.dry_run:
        lines.append("DRY RUN — nothing was written")
    lines.append(f"tenant_id={outcome.tenant_id}")

    if outcome.revoked is not None:
        r = outcome.revoked
        lines.append("revoked:")
        lines.append(f"  key_id={r.id}")
        lines.append(f"  label={r.label}")
        lines.append(f"  key_prefix={r.key_prefix}")
        lines.append(f"  environment={r.environment}")
        lines.append(f"  created_at={_timestamp(r.created_at)}")
        lines.append(f"  last_used_at={_timestamp(r.last_used_at)}")

    if outcome.minted is not None:
        lines.append("minted:")
        lines.append(f"  key_id={outcome.minted.id}")
        lines.append(f"  label={outcome.minted.label}")
        lines.append(f"  environment={outcome.minted.environment}")

    if outcome.raw_key is not None:
        lines.append(f"raw_key={outcome.raw_key}")
        lines.append("The raw key is shown ONCE. Store it in the consumer's secrets now.")
    elif outcome.minted is not None:
        lines.append("raw_key withheld — the dry run rolled this key back; it would never work")

    lines.append(f"keys_remaining={outcome.remaining_keys}")
    if outcome.remaining_keys == 0:
        lines.append("WARNING: this tenant now holds no keys and cannot authenticate at all")
    return lines


def render_list(tenant_id: str, records: list[KeyRecord]) -> list[str]:
    """Return the lines for ``--list``.

    Never prints ``key_hash``. The prefix is what identifies a key to a human;
    the hash is what verifies it, and putting an offline-crackable digest in a
    terminal buffer is not a thing a credential tool should do casually.
    """
    lines = [f"tenant_id={ulid_str(tenant_id)}"]
    if not records:
        lines.append("This tenant holds no keys and cannot authenticate at all.")
        return lines
    lines.append(f"{len(records)} key(s), oldest first:")
    for record in records:
        lines.append(f"  key_id={record.id}")
        lines.append(f"    label={record.label}")
        lines.append(f"    key_prefix={record.key_prefix}")
        lines.append(f"    environment={record.environment}")
        lines.append(f"    created_at={_timestamp(record.created_at)}")
        lines.append(f"    last_used_at={_timestamp(record.last_used_at)}")
    return lines


def verify(
    base_url: str,
    *,
    new_raw: str | None = None,
    old_raw: str | None = None,
    client: httpx.Client | None = None,
) -> list[Check]:
    """Probe *base_url* and report what the keys actually do there.

    The rotation has already committed by the time this runs, so a transport
    failure is a failed check and never an exception — a traceback here would
    leave an operator unsure whether the write landed.

    The old key's check insists on **401** specifically. A 403 means the key
    was refused for being a ``development`` key against a production
    deployment, which is a perfectly *valid* key being turned away; treating
    that as proof of revocation would certify a rotation that never happened.

    That 401 is only evidence when something in the same run proves the
    endpoint would have accepted a *good* key — on a rotation the new-key
    probe is that control. Without one, a ``--verify`` URL pointing somewhere
    that refuses everything reads as a pass, so the check says so in its own
    name rather than being quietly worth less than it looks.
    """
    owned = client is None
    client = client or httpx.Client(timeout=VERIFY_TIMEOUT_SECONDS)
    checks: list[Check] = []
    try:
        if new_raw is not None:
            checks.append(_probe(client, base_url, new_raw, "new key accepted", expected=200))
        if old_raw is not None:
            name = "old key rejected" if new_raw is not None else "old key rejected (uncontrolled)"
            checks.append(_probe(client, base_url, old_raw, name, expected=401))
    finally:
        if owned:
            client.close()
    return checks


def _probe(client: httpx.Client, base_url: str, raw_key: str, name: str, *, expected: int) -> Check:
    url = base_url.rstrip("/") + VERIFY_PATH
    try:
        response = client.get(url, headers={"X-API-Key": raw_key})
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # InvalidURL is deliberately listed: it does *not* subclass HTTPError,
        # so a fumbled base URL — `http://[::1` and this host's tailnet
        # address has an IPv6 form — escaped as a traceback, after the
        # rotation had already committed (CR 10).
        return Check(name=name, ok=False, detail=f"{url} unreachable: {exc}")
    ok = response.status_code == expected
    detail = f"{url} returned {response.status_code}, expected {expected}"
    return Check(name=name, ok=ok, detail=detail)


def _describe(args: argparse.Namespace) -> str:
    """One line naming exactly what is about to happen, for the prompt."""
    parts = []
    if args.new_label is not None:
        parts.append(f"mint a {args.environment} key labelled {args.new_label!r}")
    if args.revoke is not None:
        parts.append(f"permanently delete key {ulid_str(args.revoke)}")
    return f"About to {' and '.join(parts)} on tenant {ulid_str(args.tenant_id)}."


def _confirm(args: argparse.Namespace) -> bool:
    """Ask before writing. ``--yes`` skips; a non-terminal stdin must pass it.

    The prompt goes to stderr with the description, not through ``input``'s
    own argument, which writes to stdout. Piping stdout is the normal thing to
    do here — it is where the raw key lands, and where an operator keeping a
    record of a rotation points their `tee` — and that took the prompt into
    the pipe with it, leaving the description on screen followed by silence
    while the script waited on something invisible (CR 10, first fixed in
    ``delete_tenant.py``).

    Backing out is an abort, not a crash. Ctrl-D and Ctrl-C both raise out of
    ``input()``, and an uncaught one prints a stack trace where a refusal
    belongs — which reads like the script broke part-way through rather than
    like it declined, the ambiguity this script refuses to create anywhere
    else. Both land on the caller's "Aborted; nothing was written." (CR 14).

    ``delete_tenant.py:_confirm`` is this function's sibling and shares the
    whole contract above. The CR 10 defect existed in both because one was
    copied from the other; carry the rules across, not just the code (CR 15).
    """
    if args.yes:
        return True
    print(_describe(args), file=sys.stderr)
    if not sys.stdin.isatty():
        print(
            "Refusing to act unattended: stdin is not a terminal and --yes was not passed.",
            file=sys.stderr,
        )
        return False
    print("Type 'yes' to proceed: ", end="", file=sys.stderr)
    sys.stderr.flush()
    try:
        typed = input()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)  # the ^C or ^D sits at the end of the prompt line
        return False
    return typed.strip() == "yes"


def _open_factory() -> async_sessionmaker[AsyncSession]:
    """Resolve the session factory, or raise a refusal worth printing.

    ``get_session_factory`` runs ``DATABASE_URL`` through
    ``src/core/db_safety.py``, which raises ``ProductionDatabaseError`` without
    the ``STATUS_ALLOW_PROD_DB=1`` opt-in — and that opt-in lives in no env
    file by design, so forgetting it is the path this script is built to
    expect rather than an edge case. It arrived as six lines of stack; the
    message inside already says exactly what to do (CR 18).

    A bare ``RuntimeError`` is the unset-``DATABASE_URL`` case. It is caught
    *here*, around the one call that raises it, rather than around the work:
    ``ProductionDatabaseError`` subclasses ``RuntimeError``, so catching the
    pair anywhere wider would quietly swallow every other RuntimeError and
    report it as a refusal that wrote nothing.
    """
    return get_session_factory()


async def main(args: argparse.Namespace) -> int:
    """Run the requested operation and print the result. Returns an exit code."""
    try:
        factory = _open_factory()
    except RuntimeError as exc:  # ProductionDatabaseError is one of these
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED

    if args.list:
        try:
            async with factory() as session:
                records = await keys_for(session, args.tenant_id)
        except LookupError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return REFUSED
        for line in render_list(args.tenant_id, records):
            print(line)
        return OK

    if not args.dry_run and not _confirm(args):
        print("Aborted; nothing was written.", file=sys.stderr)
        return ABORTED

    try:
        async with factory() as session:
            outcome = await apply(
                session,
                tenant_id=args.tenant_id,
                new_label=args.new_label,
                environment=args.environment,
                revoke_id=args.revoke,
                force=args.force,
                dry_run=args.dry_run,
            )
    except (LookupError, LastKeyError, ValueError) as exc:
        # Every one of these is raised before or instead of a write, and
        # apply() has rolled back. Say so in a line: a traceback here reads
        # like the script broke rather than like it declined, and an operator
        # mid-incident resolves that ambiguity by reaching for psql.
        print(f"refused: {exc}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return REFUSED

    for line in render(outcome):
        print(line)

    if args.verify is None or outcome.dry_run:
        if args.verify is not None:
            print("verification skipped — a dry run has nothing live to check")
        elif not outcome.dry_run:
            print("NOT VERIFIED — pass --verify <base-url> to prove the new key works")
        return OK

    checks = verify(args.verify, new_raw=outcome.raw_key, old_raw=args.verify_old)
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    if not checks:
        # A revoke with no replacement leaves the script holding no raw key,
        # so --verify has nothing to send. Printing no verdict and exiting 0
        # is indistinguishable from a verification that passed, which is the
        # one thing --verify exists to rule out (CR 2). This subsumes the
        # "old key not checked" hint below — both end by asking for the same
        # flag, and saying it twice is noise at the moment output most needs
        # to be scannable (CR 13).
        print(
            "NOT VERIFIED — nothing could be checked: --verify needs a key to "
            "present, and this run minted none. Pass --verify-old <raw> to "
            "check the revoked key instead."
        )
        return NOT_VERIFIED
    if args.verify_old is None and outcome.revoked is not None:
        print(
            "old key not checked — this script only ever held its hash. "
            "Pass --verify-old <raw> to prove it now returns 401."
        )
    return OK if all(check.ok for check in checks) else NOT_VERIFIED


if __name__ == "__main__":
    # Parse first: a rejected flag combination exits here, before the audit
    # channel is opened for an operation that is not going to happen.
    _args = parse_args(sys.argv[1:])
    configure_script_logging()
    raise SystemExit(asyncio.run(main(_args)))
