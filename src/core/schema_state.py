"""Where the database stands against this code's migrations (#9, spec R8).

On 2026-09-29 the production sweep ran a model whose migration had not been
applied, and crashed on every pass for 40 minutes with a column error. This
turns that into one named failure: the database is **behind** the code.

Four states, from ``alembic_version`` against the code's single Alembic head:

- ``current``: at head.
- ``behind``: a revision this code knows, other than head. A migration is missing.
- ``ahead``: a revision this code does not know. A newer release has migrated
  and this older one is still running, which is expected in the seconds
  between migrate and switch, and after every rollback (R7). A warning, never
  a failure.
- ``unmigrated``: no revision at all.

Nothing here refuses a start (broker#22 goal 3). The sweep fails its pass, which
also sends ``/fail`` to healthchecks.io; ``/ready`` answers 503. Run as a module,
it prints the state for ``scripts/deploy.sh`` and ``scripts/dev_server.sh``, and
exits 0 (current, ahead), 3 (behind, unmigrated) or 2 (anything else: unreachable,
refused, crashed). Never 1, which is what Python exits with on an uncaught
exception, so a refusal can never read as "behind, migrate it".
"""

import asyncio
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.build import CODE_ROOT
from src.core.database import get_session_factory
from src.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

ALEMBIC_INI = CODE_ROOT / "alembic.ini"

EXIT_OK = 0
EXIT_UNREADABLE = 2
EXIT_BEHIND = 3


class SchemaState(StrEnum):
    """The database's migration state relative to this code."""

    CURRENT = "current"
    BEHIND = "behind"
    AHEAD = "ahead"
    UNMIGRATED = "unmigrated"


FAILING = frozenset({SchemaState.BEHIND, SchemaState.UNMIGRATED})


class SchemaBehind(RuntimeError):
    """The database lacks a migration this code needs: a failed pass, not a refused start."""


class MultipleHeads(RuntimeError):
    """More than one Alembic head (in the code or the database): "migrated" has no meaning."""


def script_directory(ini: Path = ALEMBIC_INI) -> ScriptDirectory:
    """The migration scripts shipped with this code."""
    return ScriptDirectory.from_config(Config(str(ini)))


@cache
def _shipped_scripts() -> ScriptDirectory:
    return script_directory()


def code_head(script: ScriptDirectory) -> str:
    """The single head revision of *script*; :class:`MultipleHeads` otherwise."""
    heads = script.get_heads()
    if len(heads) != 1:
        raise MultipleHeads(f"expected one Alembic head, found {len(heads)}: {sorted(heads)}")
    return heads[0]


def classify(db_revision: str | None, script: ScriptDirectory) -> SchemaState:
    """Pure: where *db_revision* stands against *script*'s head."""
    if db_revision is None:
        return SchemaState.UNMIGRATED
    if db_revision == code_head(script):
        return SchemaState.CURRENT
    try:
        script.get_revision(db_revision)
    except CommandError:
        return SchemaState.AHEAD
    return SchemaState.BEHIND


async def database_revision(session: AsyncSession) -> str | None:
    """The revision in ``alembic_version``, or ``None`` when there is none.

    Probes with ``to_regclass`` first: selecting from a missing table would
    abort the caller's transaction, not just this query.
    """
    exists = await session.execute(text("SELECT to_regclass('alembic_version')"))
    if exists.scalar_one() is None:
        return None
    rows = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars()
    revisions = list(rows)
    if len(revisions) > 1:
        raise MultipleHeads(f"the database is stamped at {len(revisions)} heads: {revisions}")
    return revisions[0] if revisions else None


async def schema_state(session: AsyncSession) -> SchemaState:
    """The database behind *session* against the code that is running."""
    return classify(await database_revision(session), _shipped_scripts())


async def require_current(session: AsyncSession) -> SchemaState:
    """Raise :class:`SchemaBehind` unless current or ahead; warn when ahead."""
    script = _shipped_scripts()
    revision = await database_revision(session)
    state = classify(revision, script)
    if state in FAILING:
        raise SchemaBehind(
            f"database schema is {state}: at {revision or 'no revision'}, "
            f"this code needs {code_head(script)}. Run the deploy's migration step."
        )
    if state is SchemaState.AHEAD:
        logger.warning(
            f"database schema is ahead of this code: at {revision}, code head "
            f"{code_head(script)}. Expected only mid-deploy or after a rollback."
        )
    return state


async def main(
    factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
) -> int:
    """Print the state; exit 0 (current, ahead), 3 (behind, unmigrated), 2 (anything else).

    Every failure is 2 with nothing on stdout: ``db_safety`` refusing the URL,
    a missing ``DATABASE_URL``, two heads. A caller that migrates on "behind"
    must never mistake one of those for it (CR 1).
    """
    try:
        async with (factory or get_session_factory())() as session:
            revision = await database_revision(session)
        script = _shipped_scripts()
        state = classify(revision, script)
        head = code_head(script)
    except (SQLAlchemyError, OSError) as exc:
        # The type only: a driver message can carry the connection string.
        print(f"schema_state: cannot reach the database: {type(exc).__name__}", file=sys.stderr)
        return EXIT_UNREADABLE
    except Exception as exc:
        print(
            f"schema_state: cannot read the schema state: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_UNREADABLE
    if state is SchemaState.AHEAD:
        # "A revision this code does not know" is a newer release, or a branch
        # migration that was deployed to dev and never merged (CR 7).
        print(
            f"schema_state: database at {revision}, unknown to this code (head {head}): "
            "a newer release, or a branch migration never merged",
            file=sys.stderr,
        )
    print(state.value)
    return EXIT_BEHIND if state in FAILING else EXIT_OK


if __name__ == "__main__":
    # stdout carries the state alone; the shell scripts read it.
    configure_logging(stream=sys.stderr)
    sys.exit(asyncio.run(main()))
