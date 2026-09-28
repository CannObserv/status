"""Tests for src/core/api_keys.py.

The minting half existed only inside ``scripts/seed_tenant.py``, where
``[tool.coverage.run] source = ["src"]`` cannot see it — credential logic was
the one part of this service the 80% gate never measured (notifier#62). Moving it here
buys the gate, one spelling of the hash the auth path already computes, and a
revoke path that can be tested without a subprocess.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select

from src.core.api_keys import (
    RAW_KEY_PREFIX,
    KeyNotFoundError,
    KeyOwnershipError,
    LastKeyError,
    TenantNotFoundError,
    generate_raw_key,
    hash_key,
    key_count,
    keys_for,
    mint,
    revoke,
    ulid_str,
)
from src.core.logging import AUDIT_LOGGER_NAME
from src.core.models import ApiKey, Tenant


def _audit(caplog) -> list[logging.LogRecord]:
    """The audit records captured so far, and nothing else."""
    return [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]


async def _count(session, tenant_id: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(ApiKey).where(ApiKey.tenant_id == tenant_id)
    )
    return int(result.scalar_one())


class TestHashKey:
    def test_matches_the_spelling_the_auth_path_uses(self):
        """src/api/deps.py looks a key up by this digest. A second spelling
        here would be a credential that mints but cannot authenticate."""
        raw = "csk_whatever"
        assert hash_key(raw) == hashlib.sha256(raw.encode()).hexdigest()

    def test_is_deterministic(self):
        assert hash_key("csk_abc") == hash_key("csk_abc")

    def test_distinct_keys_hash_distinctly(self):
        assert hash_key("csk_abc") != hash_key("csk_abd")


class TestGenerateRawKey:
    def test_carries_the_recognisable_prefix(self):
        assert generate_raw_key().startswith(RAW_KEY_PREFIX)

    def test_every_call_differs(self):
        assert len({generate_raw_key() for _ in range(50)}) == 50

    def test_carries_at_least_256_bits_of_entropy(self):
        """token_urlsafe(32) is 32 random bytes; base64 renders them in 43
        characters. A shorter key would be a silent downgrade."""
        assert len(generate_raw_key()) >= len(RAW_KEY_PREFIX) + 43


class TestMint:
    async def test_persists_a_key_for_the_tenant(self, db_session, tenant):
        key, _raw = await mint(db_session, tenant.id, "label-a", "production")

        assert key.tenant_id == tenant.id
        assert key.label == "label-a"
        assert key.environment == "production"
        assert await _count(db_session, tenant.id) == 1

    async def test_returns_a_raw_key_that_authenticates(self, db_session, tenant):
        key, raw = await mint(db_session, tenant.id, "label-a", "production")

        assert key.key_hash == hash_key(raw)
        assert key.key_prefix == raw[:8]

    async def test_never_persists_the_raw_key(self, db_session, tenant):
        """Only the digest is stored. A raw key recoverable from the row would
        make the database a credential store rather than a verifier."""
        _key, raw = await mint(db_session, tenant.id, "label-a", "production")

        row = (
            (await db_session.execute(select(ApiKey).where(ApiKey.tenant_id == tenant.id)))
            .scalars()
            .one()
        )
        stored = (row.label, row.key_prefix, row.key_hash, row.environment)
        assert not any(raw in field for field in stored)

    async def test_mints_a_development_key(self, db_session, tenant):
        key, _raw = await mint(db_session, tenant.id, "label-a", "development")

        assert key.environment == "development"

    async def test_refuses_an_unknown_environment(self, db_session, tenant):
        """The model's CheckConstraint would catch this at flush with a
        database error naming a constraint. Refuse it by name instead."""
        with pytest.raises(ValueError, match="staging"):
            await mint(db_session, tenant.id, "label-a", "staging")
        assert await _count(db_session, tenant.id) == 0

    async def test_refuses_an_unknown_tenant(self, db_session):
        """The foreign key would raise at flush. This names the tenant id the
        operator actually typed."""
        with pytest.raises(TenantNotFoundError, match="01BOGUSTENANTID0000000000"):
            await mint(db_session, "01BOGUSTENANTID0000000000", "label-a", "production")

    async def test_a_second_key_joins_the_first(self, db_session, tenant):
        """The gap notifier#62 opens with: seed_tenant.py could only ever create a
        tenant, so a consumer needing two credentials had no path."""
        await mint(db_session, tenant.id, "label-a", "production")
        await mint(db_session, tenant.id, "label-b", "production")

        assert await _count(db_session, tenant.id) == 2

    async def test_does_not_commit(self, db_session, tenant):
        """The caller owns the transaction, which is what lets a rotation put
        the mint and the revoke in one."""
        await mint(db_session, tenant.id, "label-a", "production")
        await db_session.rollback()

        assert await _count(db_session, tenant.id) == 0


class TestKeyCount:
    async def test_counts_only_this_tenant(self, db_session, tenant):
        other = Tenant(name="other-tenant-for-count")
        db_session.add(other)
        await db_session.flush()
        await mint(db_session, tenant.id, "label-a", "production")
        await mint(db_session, other.id, "label-b", "production")
        await mint(db_session, other.id, "label-c", "production")

        assert await key_count(db_session, tenant.id) == 1
        assert await key_count(db_session, other.id) == 2


class TestRevoke:
    async def test_removes_the_row(self, db_session, tenant):
        doomed, _ = await mint(db_session, tenant.id, "old", "production")
        await mint(db_session, tenant.id, "new", "production")

        await revoke(db_session, tenant.id, doomed.id)

        remaining = (
            (await db_session.execute(select(ApiKey).where(ApiKey.tenant_id == tenant.id)))
            .scalars()
            .all()
        )
        assert [r.label for r in remaining] == ["new"]

    async def test_returns_a_snapshot_of_what_it_deleted(self, db_session, tenant):
        """The operator has to be able to read back which key just died —
        the confirmation the ad-hoc notifier#57 rotation had no way to print."""
        doomed, raw = await mint(db_session, tenant.id, "old", "production")
        doomed.last_used_at = datetime(2026, 9, 12, 4, 30, tzinfo=UTC)
        await mint(db_session, tenant.id, "new", "production")

        snapshot = await revoke(db_session, tenant.id, doomed.id)

        assert snapshot.label == "old"
        assert snapshot.key_prefix == raw[:8]
        assert snapshot.environment == "production"
        assert snapshot.last_used_at == datetime(2026, 9, 12, 4, 30, tzinfo=UTC)

    async def test_refuses_an_unknown_key(self, db_session, tenant):
        await mint(db_session, tenant.id, "a", "production")
        await mint(db_session, tenant.id, "b", "production")

        with pytest.raises(KeyNotFoundError, match="01BOGUSKEYID000000000000"):
            await revoke(db_session, tenant.id, "01BOGUSKEYID000000000000")
        assert await _count(db_session, tenant.id) == 2

    async def test_refuses_a_key_belonging_to_another_tenant(self, db_session, tenant):
        """One mistyped ULID must not be able to revoke a different
        consumer's credential."""
        victim = Tenant(name="innocent-bystander")
        db_session.add(victim)
        await db_session.flush()
        theirs, _ = await mint(db_session, victim.id, "theirs", "production")
        await mint(db_session, victim.id, "theirs-2", "production")

        with pytest.raises(KeyOwnershipError) as exc:
            await revoke(db_session, tenant.id, theirs.id)

        assert str(victim.id) in str(exc.value)
        assert str(tenant.id) in str(exc.value)
        assert await _count(db_session, victim.id) == 2

    async def test_refuses_the_tenants_last_key(self, db_session, tenant):
        """A tenant with zero keys is a consumer that is down, and the sweep
        reports the monitor missing without saying why."""
        only, _ = await mint(db_session, tenant.id, "only", "production")

        with pytest.raises(LastKeyError, match="last"):
            await revoke(db_session, tenant.id, only.id)
        assert await _count(db_session, tenant.id) == 1

    async def test_allow_last_overrides_the_refusal(self, db_session, tenant):
        only, _ = await mint(db_session, tenant.id, "only", "production")

        await revoke(db_session, tenant.id, only.id, allow_last=True)

        assert await _count(db_session, tenant.id) == 0

    async def test_a_freshly_minted_replacement_makes_the_old_key_not_the_last(
        self, db_session, tenant
    ):
        """The last-key guard must not fire on a rotation. Mint flushes before
        revoke counts, so the tenant is never at zero and --force is never
        reachable on this path."""
        old, _ = await mint(db_session, tenant.id, "old", "production")
        await mint(db_session, tenant.id, "new", "production")

        await revoke(db_session, tenant.id, old.id)

        assert await _count(db_session, tenant.id) == 1

    async def test_accepts_a_tenant_id_as_a_plain_string(self, db_session, tenant):
        """An operator's argv holds a str; the row holds a ulid.ULID. A
        tenancy check that leaned on those comparing equal would refuse every
        revoke the day that stopped being true."""
        old, _ = await mint(db_session, tenant.id, "old", "production")
        await mint(db_session, tenant.id, "new", "production")

        snapshot = await revoke(db_session, str(tenant.id), str(old.id))

        assert snapshot.label == "old"

    async def test_the_ownership_message_renders_bare_ulids(self, db_session, tenant):
        """`ULID(01J...)` in an incident message is one more thing for a tired
        operator to misread against the id they typed."""
        victim = Tenant(name="bystander-for-message")
        db_session.add(victim)
        await db_session.flush()
        theirs, _ = await mint(db_session, victim.id, "theirs", "production")
        await mint(db_session, victim.id, "theirs-2", "production")

        with pytest.raises(KeyOwnershipError) as exc:
            await revoke(db_session, tenant.id, theirs.id)

        assert "ULID(" not in str(exc.value)


class TestRotationAtomicity:
    async def test_an_abandoned_rotation_leaves_the_original_key_intact(self, db_session, tenant):
        """Neither mint nor revoke commits, so a rotation that dies partway
        through cannot leave a tenant holding a key nobody has. This is the
        whole reason the two halves share a transaction."""
        original, original_raw = await mint(db_session, tenant.id, "original", "production")
        original_id = str(original.id)

        rotation = await db_session.begin_nested()
        await mint(db_session, tenant.id, "replacement", "production")
        await revoke(db_session, tenant.id, original_id)
        await rotation.rollback()

        surviving = (
            (await db_session.execute(select(ApiKey).where(ApiKey.tenant_id == tenant.id)))
            .scalars()
            .all()
        )
        assert [r.label for r in surviving] == ["original"]
        assert surviving[0].key_hash == hash_key(original_raw)


class TestUlidStr:
    def test_renders_a_ulid_without_its_wrapper(self, tenant):
        assert ulid_str(tenant.id) == str(tenant.id)
        assert "ULID(" not in ulid_str(tenant.id)

    def test_passes_a_string_through(self):
        assert ulid_str("01J0000000000000000000000") == "01J0000000000000000000000"


class TestKeysFor:
    async def test_lists_this_tenants_keys_oldest_first(self, db_session, tenant):
        """The operator needs the key *id* to revoke one, and until this
        existed there was no sanctioned way to read it — which sends them
        back to the ad-hoc SQL notifier#62 is trying to retire."""
        await mint(db_session, tenant.id, "first", "production")
        await mint(db_session, tenant.id, "second", "development")

        records = await keys_for(db_session, tenant.id)

        assert [r.label for r in records] == ["first", "second"]
        assert [r.environment for r in records] == ["production", "development"]

    async def test_excludes_other_tenants(self, db_session, tenant):
        other = Tenant(name="other-tenant-for-listing")
        db_session.add(other)
        await db_session.flush()
        await mint(db_session, tenant.id, "mine", "production")
        await mint(db_session, other.id, "theirs", "production")

        assert [r.label for r in await keys_for(db_session, tenant.id)] == ["mine"]

    async def test_renders_bare_ulids(self, db_session, tenant):
        await mint(db_session, tenant.id, "first", "production")

        record = (await keys_for(db_session, tenant.id))[0]

        assert "ULID(" not in record.id
        assert "ULID(" not in record.tenant_id

    async def test_refuses_an_unknown_tenant(self, db_session):
        """An empty list for a mistyped id reads identically to a tenant that
        genuinely holds no keys — the difference between a typo and a consumer
        that is already down."""
        with pytest.raises(TenantNotFoundError):
            await keys_for(db_session, "01BOGUSTENANTID0000000000")


class TestLastKeyCheckIsSerialized:
    async def test_revoke_locks_the_tenant_row(self, db_session, tenant):
        """Two concurrent revokes could each read a count of 2 and each
        delete, landing the tenant at zero keys — the outcome the guard
        exists to prevent and that --force exists to make deliberate.
        Locking the tenant row makes the second wait for the first's
        commit, so it counts 1 and refuses (CR 7).
        """
        await mint(db_session, tenant.id, "a", "production")
        await mint(db_session, tenant.id, "b", "production")

        statements = []

        @event.listens_for(db_session.sync_session, "do_orm_execute")
        def record(orm_context):
            statements.append(str(orm_context.statement).lower())

        key_b = (
            (await db_session.execute(select(ApiKey).where(ApiKey.label == "b"))).scalars().one()
        )
        await revoke(db_session, tenant.id, key_b.id)

        event.remove(db_session.sync_session, "do_orm_execute", record)
        assert any("for update" in s and "tenants" in s for s in statements), statements

    async def test_force_takes_no_lock(self, db_session, tenant):
        """Under allow_last the delete is unconditional: no count is read, so
        there is no read-then-write to serialize. A lock that protects nothing
        invites the next reader to assume it protects something (CR 16)."""
        only, _ = await mint(db_session, tenant.id, "only", "production")

        statements = []

        @event.listens_for(db_session.sync_session, "do_orm_execute")
        def record(orm_context):
            statements.append(str(orm_context.statement).lower())

        await revoke(db_session, tenant.id, only.id, allow_last=True)

        event.remove(db_session.sync_session, "do_orm_execute", record)
        assert not any("for update" in s for s in statements), statements


class TestAuditRecords:
    """Every mint and every revoke names the key, on the audit channel (notifier#67).

    notifier#62 declined a `revoked_at` column — DELETE fails closed where a filter
    everyone must remember fails open — and paid for the lost audit trail with
    a log line here. The line existed and had never once been emitted: it went
    to `src.core.api_keys`, whose records the two credential scripts dropped on
    the floor. A revoke left nothing at all, the row and its going both gone.

    Asserting on the logger *name* as well as the payload: routing these back
    through the module logger renders identically today and silently leaves the
    journal again the moment an entry point configures only the root.
    """

    async def test_mint_records_the_key_it_made(self, db_session, tenant, caplog):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

        key, _ = await mint(db_session, tenant.id, "nightly backup", "development")

        (record,) = _audit(caplog)
        assert record.message == "api key minted"
        assert record.tenant_id == str(tenant.id)
        assert record.key_id == str(key.id)
        assert record.key_prefix == key.key_prefix
        assert record.label == "nightly backup"
        assert record.environment == "development"

    async def test_mint_never_records_the_raw_key(self, db_session, tenant, caplog):
        """The prefix identifies the key to a human; the secret is the one
        thing a durable record must never hold."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

        _, raw = await mint(db_session, tenant.id, "smoke", "production")

        (record,) = _audit(caplog)
        assert raw not in json.dumps(record.__dict__, default=str)

    async def test_revoke_records_what_went(self, db_session, tenant, caplog):
        """The row is gone after this; the record is all there is."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        key, _ = await mint(db_session, tenant.id, "doomed", "production")
        await mint(db_session, tenant.id, "survivor", "production")
        caplog.clear()  # the setup's own mints are not what this asserts on

        await revoke(db_session, tenant.id, key.id)

        (record,) = _audit(caplog)
        assert record.message == "api key revoked"
        assert record.tenant_id == str(tenant.id)
        assert record.key_id == str(key.id)
        assert record.key_prefix == key.key_prefix
        assert record.label == "doomed"
        assert record.environment == "production"

    async def test_a_refused_revoke_records_nothing(self, db_session, tenant, caplog):
        """The last-key guard writes nothing, so it must claim nothing."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        key, _ = await mint(db_session, tenant.id, "only", "production")
        caplog.clear()

        with pytest.raises(LastKeyError):
            await revoke(db_session, tenant.id, key.id)

        assert _audit(caplog) == []
