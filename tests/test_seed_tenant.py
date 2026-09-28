"""Tests for scripts/seed_tenant.py.

Mostly about its *output*, which is an interface with two consumers: an
operator copying a line into a consumer's secrets, and
``clients/python/tests/conftest.py``, which shells this script and parses
stdout with `tenant_id=(\\S+)` / `raw_key=(\\S+)`. Nothing failed loudly when
that shape drifted — the SDK's integration fixture just stopped being able to
seed, one layer away from the change that broke it.
"""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import scripts.seed_tenant as seed_tenant_module
from scripts.seed_tenant import main
from src.core.api_keys import hash_key
from src.core.models import ApiKey, Tenant

#: Transcribed from clients/python/tests/conftest.py. A copy, deliberately:
#: the point is to fail here when seed_tenant's output stops satisfying them.
SDK_TENANT_PATTERN = re.compile(r"tenant_id=(\S+)")
SDK_KEY_PATTERN = re.compile(r"raw_key=(\S+)")


@pytest.fixture
def seeded(capsys, monkeypatch, test_engine):
    """Run ``main`` against the test database and return its parsed output."""
    monkeypatch.setattr(
        seed_tenant_module,
        "get_session_factory",
        lambda: async_sessionmaker(test_engine, expire_on_commit=False),
    )
    return capsys


class TestOutput:
    async def test_prints_what_the_sdk_fixture_parses(self, seeded):
        await main("seed-output-tenant", "smoke", "production")

        out = seeded.readouterr().out
        assert SDK_TENANT_PATTERN.search(out), out
        assert SDK_KEY_PATTERN.search(out), out

    async def test_prints_the_key_id(self, seeded):
        """rotate_key.py --revoke takes a key id. Minting one without ever
        printing it is what sends the next operator to ad-hoc SQL to find it
        (notifier#62)."""
        await main("seed-key-id-tenant", "smoke", "production")

        assert re.search(r"key_id=(\S+)", seeded.readouterr().out)

    async def test_shows_the_raw_key_exactly_once(self, seeded):
        seeded.readouterr()  # drain anything an earlier assertion left
        await main("seed-once-tenant", "smoke", "production")

        out = seeded.readouterr().out
        raw = SDK_KEY_PATTERN.search(out).group(1)
        assert out.count(raw) == 1


class TestPersistence:
    async def test_stores_only_the_hash(self, seeded, db_session):
        await main("seed-hash-tenant", "smoke", "production")
        raw = SDK_KEY_PATTERN.search(seeded.readouterr().out).group(1)

        row = (
            (await db_session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw))))
            .scalars()
            .one()
        )
        assert raw not in (row.key_prefix + row.label + row.key_hash)

    async def test_creates_a_new_tenant_every_time(self, seeded, db_session):
        """It only ever creates. Attaching a key to a tenant that already
        exists is rotate_key.py's job, and keeping the two apart is why a
        script named seed_tenant never deletes a credential."""
        await main("seed-new-tenant-a", "smoke", "production")
        await main("seed-new-tenant-b", "smoke", "production")

        names = (
            (
                await db_session.execute(
                    select(Tenant.name).where(Tenant.name.like("seed-new-tenant-%"))
                )
            )
            .scalars()
            .all()
        )
        assert sorted(names) == ["seed-new-tenant-a", "seed-new-tenant-b"]


class TestAuditChannel:
    """A mint run by hand leaves a record that outlives the shell (notifier#67).

    Run as a subprocess, against a socket bound by the test, because the wiring
    under test lives in ``if __name__ == "__main__"`` and the records have to
    survive leaving the process to be worth anything.
    """

    def test_a_mint_leaves_a_record_on_the_audit_channel(self, run_script, audit_socket):
        done = run_script("seed_tenant.py", "seed-audit-tenant", "nightly", "development")
        assert done.returncode == 0, done.stderr

        record = audit_socket.records()[0]
        assert record["message"] == "api key minted"
        assert record["key_id"] == re.search(r"key_id=(\S+)", done.stdout).group(1)
        assert record["tenant_id"] == SDK_TENANT_PATTERN.search(done.stdout).group(1)
        assert record["label"] == "nightly"
        assert record["environment"] == "development"

    def test_the_record_never_carries_the_raw_key(self, run_script, audit_socket):
        """The same rule `test_shows_the_raw_key_exactly_once` holds for stdout,
        extended to the durable channel: once in the operator's hands, never in
        a record that persists (notifier#67)."""
        done = run_script("seed_tenant.py", "seed-audit-secret", "smoke", "production")
        raw = SDK_KEY_PATTERN.search(done.stdout).group(1)

        emitted = audit_socket.datagrams() + [done.stderr]
        assert done.stdout.count(raw) == 1
        assert not [line for line in emitted if raw in line]

    def test_stdout_stays_the_operator_channel(self, run_script, audit_socket):
        """The trap in the obvious fix: `configure_logging()` binds stdout, so
        adding it naively interleaves JSON with the two lines
        `clients/python/tests/conftest.py` parses and the line an operator
        pastes into a consumer's secrets."""
        done = run_script("seed_tenant.py", "seed-audit-stdout", "smoke", "production")

        assert SDK_TENANT_PATTERN.search(done.stdout), done.stdout
        assert SDK_KEY_PATTERN.search(done.stdout), done.stdout
        assert [line.split("=")[0] for line in done.stdout.splitlines()] == [
            "tenant_id",
            "key_id",
            "raw_key",
            "environment",
        ]
        audit_socket.records()  # the record went to the journal instead
