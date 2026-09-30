"""Tests for src/core/schema_state.py — the database against the code's head (#9, R8).

On 2026-09-29 the production sweep ran a model whose migration had not been
applied, and crashed on every pass for 40 minutes with a column error. The check
turns that into one named failure, ``SchemaBehind``. It deliberately lets a
database *ahead* of the code through: that is the old release in the seconds
between migrate and switch, and every rollback (R7, R9).

Classified against the repo's real ``alembic/``, so a new migration needs no
edit here: the oldest revision is always behind, the head always current.
"""

import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

from src.core import schema_state
from src.core.schema_state import SchemaBehind, SchemaState

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def script():
    return schema_state.script_directory()


@pytest.fixture(scope="module")
def head(script):
    return schema_state.code_head(script)


@pytest.fixture(scope="module")
def oldest(script):
    return script.get_base()


class TestClassify:
    def test_at_head_is_current(self, script, head):
        assert schema_state.classify(head, script) is SchemaState.CURRENT

    def test_a_known_older_revision_is_behind(self, script, oldest, head):
        assert oldest != head, "needs at least two migrations"
        assert schema_state.classify(oldest, script) is SchemaState.BEHIND

    def test_a_revision_the_code_does_not_know_is_ahead(self, script):
        """A newer release migrated, and this older code is still running."""
        assert schema_state.classify("ffffffffffff", script) is SchemaState.AHEAD

    def test_no_revision_is_unmigrated(self, script):
        assert schema_state.classify(None, script) is SchemaState.UNMIGRATED


class TestCodeHead:
    def test_the_repo_has_exactly_one_head(self, script):
        assert len(script.get_heads()) == 1

    def test_two_heads_are_refused(self, tmp_path):
        """A deploy cannot say what "migrated" means with two heads, so it stops."""
        ini = tmp_path / "alembic.ini"
        versions = tmp_path / "alembic" / "versions"
        versions.mkdir(parents=True)
        (tmp_path / "alembic" / "script.py.mako").write_text("")
        ini.write_text("[alembic]\nscript_location = %(here)s/alembic\n")
        for rev in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
            (versions / f"{rev}_x.py").write_text(
                f"revision = '{rev}'\ndown_revision = None\n"
                "branch_labels = None\ndepends_on = None\n"
            )
        with pytest.raises(schema_state.MultipleHeads):
            schema_state.code_head(schema_state.script_directory(ini))


class TestDatabaseRevision:
    async def test_reads_the_stamped_revision(self, db_session, head):
        """conftest stamps the test database at head, as a migrated one would be."""
        assert await schema_state.database_revision(db_session) == head

    async def test_no_table_is_none_and_leaves_the_transaction_usable(self, db_session):
        """``to_regclass``, not a failing SELECT, which would abort the transaction."""
        await db_session.execute(text("DROP TABLE alembic_version"))
        assert await schema_state.database_revision(db_session) is None
        assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1

    async def test_an_empty_table_is_none(self, db_session):
        """What ``alembic downgrade base`` leaves behind."""
        await db_session.execute(text("DELETE FROM alembic_version"))
        assert await schema_state.database_revision(db_session) is None


class TestRequireCurrent:
    async def test_current_passes(self, db_session):
        assert await schema_state.require_current(db_session) is SchemaState.CURRENT

    async def test_behind_raises_naming_both_revisions(self, db_session, oldest, head):
        await db_session.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": oldest})
        with pytest.raises(SchemaBehind) as raised:
            await schema_state.require_current(db_session)
        assert oldest in str(raised.value)
        assert head in str(raised.value)

    async def test_unmigrated_raises(self, db_session):
        await db_session.execute(text("DELETE FROM alembic_version"))
        with pytest.raises(SchemaBehind):
            await schema_state.require_current(db_session)

    async def test_ahead_warns_and_passes(self, db_session, caplog):
        """The old release during migrate-then-switch, or after a rollback."""
        await db_session.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
        with caplog.at_level("WARNING"):
            assert await schema_state.require_current(db_session) is SchemaState.AHEAD
        assert any("ffffffffffff" in r.getMessage() for r in caplog.records)


def _factory_for(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class TestMain:
    """The CLI deploy.sh and dev_server.sh read: state on stdout, exit 0/3/2.

    Behind is 3, not 1: Python exits 1 on any uncaught exception, and a caller
    that read 1 as "behind" would migrate a database the guard had refused.
    """

    async def test_current_exits_0(self, db_session, capsys):
        assert await schema_state.main(_factory_for(db_session)) == 0
        assert capsys.readouterr().out.strip() == "current"

    async def test_ahead_exits_0(self, db_session, capsys):
        await db_session.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
        assert await schema_state.main(_factory_for(db_session)) == 0
        assert capsys.readouterr().out.strip() == "ahead"

    async def test_behind_exits_3(self, db_session, oldest, capsys):
        await db_session.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": oldest})
        assert await schema_state.main(_factory_for(db_session)) == 3
        assert capsys.readouterr().out.strip() == "behind"

    async def test_unmigrated_exits_3(self, db_session, capsys):
        await db_session.execute(text("DROP TABLE alembic_version"))
        assert await schema_state.main(_factory_for(db_session)) == 3
        assert capsys.readouterr().out.strip() == "unmigrated"

    async def test_any_other_failure_exits_2_and_prints_no_state(self, capsys):
        """CR 1: a refusal or crash must never read as "behind"."""

        def refusing_factory():
            raise RuntimeError("DATABASE_URL environment variable is not set")

        assert await schema_state.main(refusing_factory) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "RuntimeError" in captured.err


def _run_module(database_url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "src.core.schema_state"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_module_runs_as_a_script(test_engine):
    """The entry point itself, as the shell scripts call it."""
    result = _run_module(os.environ["TEST_DATABASE_URL"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "current"


def test_a_refused_production_url_exits_2_not_behind():
    """CR 1: db_safety's refusal, the case that would migrate production."""
    env = {k: v for k, v in os.environ.items() if k != "STATUS_ALLOW_PROD_DB"}
    result = subprocess.run(
        [sys.executable, "-m", "src.core.schema_state"],
        cwd=REPO_ROOT,
        env={**env, "DATABASE_URL": "postgresql+asyncpg://u@127.0.0.1:1/status"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "ProductionDatabaseError" in result.stderr


def test_an_unreachable_database_exits_2():
    """Distinct from "behind": the deploy's message has to say which."""
    result = _run_module("postgresql+asyncpg://nobody@127.0.0.1:1/nothing_test")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot reach" in result.stderr
