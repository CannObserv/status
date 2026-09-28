"""Import one monitor exported from notifier (spec § Cutover, step 2).

Usage (production — deliberate, opt-in)::

    . scripts/load_env.sh

    # Rehearse first: every refusal fires, nothing is written
    STATUS_ALLOW_PROD_DB=1 uv run python scripts/import_monitors.py \\
        --row co-broker.json --expect-tenant co-broker \\
        --channel <old channel id>=<new channel id> [--channel ...] --dry-run

    # Then the same command line without --dry-run

``--row`` is one JSON object, printed by the read-only export query in
``docs/RUNBOOK.md``. Each ``--channel`` pairs a ``source_channel_id`` with the
``channel_id`` notifier's ``scripts/copy_channels.py`` printed for its copy.

``--expect-tenant`` is required and is checked against the row before the
database is opened: the row names the tenant, and a hand-edited export must
not land a monitor on another consumer. It names the co-status tenant, after
the ``watcher`` → ``co-watcher`` rename.

The monitor arrives disabled. Enabling it is the handover (spec § Cutover,
step 5), after its consumer's first check-in has landed here.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from src.core.database import get_session_factory
from src.core.importer import ImportedMonitor, ImportRefused, import_monitor, target_tenant_name
from src.core.logging import configure_logging

#: Exit codes, spelled as in delete_tenant.py and rotate_key.py.
OK = 0
REFUSED = 2


def _channel_spec(value: str) -> tuple[str, str]:
    """Parse ``OLD_CHANNEL_ID=NEW_CHANNEL_ID``."""
    old, sep, new = value.partition("=")
    if not sep or not old.strip() or not new.strip():
        raise argparse.ArgumentTypeError(f"expected OLD_ID=NEW_ID, got {value!r}")
    return old.strip(), new.strip()


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="import_monitors.py",
        description="Import one monitor exported from notifier, disabled.",
    )
    parser.add_argument("--row", required=True, type=Path, help="the exported row (JSON)")
    parser.add_argument(
        "--expect-tenant", required=True, help="the co-status tenant the row must land on"
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        type=_channel_spec,
        help="OLD_ID=NEW_ID for each of the monitor's channels (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="rehearse; write nothing")
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse *argv*."""
    return build_parser().parse_args(argv)


def render(imported: ImportedMonitor, dry_run: bool) -> list[str]:
    """Return the lines to print."""
    lines = ["DRY RUN — nothing was written"] if dry_run else []
    lines += [
        f"monitor_id={imported.id}",
        f"tenant={imported.tenant_name}",
        f"name={imported.name}",
        f"state={imported.state}",
        f"last_checkin_at={imported.last_checkin_at or 'never'}",
        f"channel_ids={','.join(imported.channel_ids)}",
        "enabled=false",
    ]
    return lines


async def main(args: argparse.Namespace) -> int:
    """Import, print, and exit ``OK`` or ``REFUSED``."""
    row = json.loads(args.row.read_text())
    target = target_tenant_name(row)
    if target != args.expect_tenant:
        print(
            f"import_monitors: the row lands on tenant {target!r}, "
            f"not {args.expect_tenant!r}; refusing",
            file=sys.stderr,
        )
        return REFUSED
    factory = get_session_factory()
    async with factory() as session:
        try:
            imported = await import_monitor(session, row, dict(args.channel), dry_run=args.dry_run)
        except ImportRefused as exc:
            print(f"import_monitors: {exc}", file=sys.stderr)
            return REFUSED
    print("\n".join(render(imported, args.dry_run)))
    return OK


if __name__ == "__main__":
    configure_logging()
    sys.exit(asyncio.run(main(parse_args(sys.argv[1:]))))
