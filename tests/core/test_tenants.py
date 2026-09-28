"""Tests for src/core/tenants.py.

Deleting a tenant destroys more than a revoke does — every credential the
consumer holds goes at once — and in notifier, until notifier#79, it left
nothing at all on the audit channel: the keys go by ``ON DELETE CASCADE``,
never through ``revoke()``. So most of what is asserted here is the record, and
that a rehearsal leaves neither rows nor records.
"""

import json
import logging

import pytest
from sqlalchemy import event, func, select

from src.core.api_keys import TenantNotFoundError, mint
from src.core.logging import AUDIT_LOGGER_NAME
from src.core.models import ApiKey, Tenant
from src.core.tenants import TenantInventory, delete_tenant, inventory_of


def _audit(caplog) -> list[logging.LogRecord]:
    """The audit records captured so far, and nothing else."""
    return [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]


async def _rows(session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in where.items():
        stmt = stmt.where(getattr(model, column) == value)
    result = await session.execute(stmt)
    return int(result.scalar_one())


@pytest.fixture
async def committed(db_session):
    """A tenant that has really been written, and its id.

    ``delete_tenant`` owns its transaction — it rolls back a rehearsal and
    commits a real run — and a rollback discards anything the fixture had
    merely flushed. Committing first puts the setup rows on the far side of
    that boundary, where the savepointed session still tears them down.

    The id as a string, not the ORM instance: after the rollback a rehearsal
    performs, reading ``tenant.id`` is a lazy load against an expired
    attribute, and the failure names SQLAlchemy internals rather than anything
    under test.
    """
    tenant = Tenant(name="doomed-tenant")
    db_session.add(tenant)
    await db_session.commit()
    return str(tenant.id)


async def _furnish(session, tenant_id: str) -> None:
    """Give *tenant_id* one of everything a delete has to take with it."""
    await mint(session, tenant_id, "first", "production")
    await mint(session, tenant_id, "second", "development")
    await session.commit()


class TestInventory:
    async def test_names_what_a_delete_would_destroy(self, db_session, committed):
        await _furnish(db_session, committed)

        inventory = await inventory_of(db_session, committed)

        assert inventory.tenant_id == committed
        assert inventory.tenant_name == "doomed-tenant"
        assert [key.label for key in inventory.keys] == ["first", "second"]

    async def test_reads_nothing_into_existence(self, db_session, committed):
        """An empty tenant is an inventory of zeroes, not a refusal."""
        inventory = await inventory_of(db_session, committed)

        assert inventory.keys == []

    async def test_refuses_a_tenant_that_does_not_exist(self, db_session):
        """A typo'd ULID must not read as "a tenant with nothing in it"."""
        with pytest.raises(TenantNotFoundError):
            await inventory_of(db_session, "01J0NOSUCHTENANT0000000000")

    async def test_is_read_only(self, db_session, committed):
        await _furnish(db_session, committed)

        await inventory_of(db_session, committed)

        assert await _rows(db_session, Tenant, id=committed) == 1
        assert await _rows(db_session, ApiKey, tenant_id=committed) == 2


class TestDeleteTenant:
    async def test_takes_the_tenant_and_its_keys(self, db_session, committed):
        await _furnish(db_session, committed)

        await delete_tenant(db_session, committed, dry_run=False)

        assert await _rows(db_session, Tenant, id=committed) == 0
        assert await _rows(db_session, ApiKey, tenant_id=committed) == 0

    async def test_returns_what_it_destroyed(self, db_session, committed):
        await _furnish(db_session, committed)

        inventory = await delete_tenant(db_session, committed, dry_run=False)

        assert isinstance(inventory, TenantInventory)
        assert [key.label for key in inventory.keys] == ["first", "second"]
        assert inventory.tenant_name == "doomed-tenant"

    async def test_refuses_a_tenant_that_does_not_exist(self, db_session):
        with pytest.raises(TenantNotFoundError):
            await delete_tenant(db_session, "01J0NOSUCHTENANT0000000000", dry_run=False)

    async def test_defaults_to_rehearsing(self, db_session, committed):
        """A caller that forgets the flag writes nothing, the way
        ``rotate_key.apply`` does."""
        await delete_tenant(db_session, committed)

        assert await _rows(db_session, Tenant, id=committed) == 1

    async def test_locks_the_tenant_row_before_reading_the_inventory(self, db_session, committed):
        """The inventory read and the delete are two statements, and a key
        minted in the gap between them is taken by the cascade while being
        absent from the snapshot the audit records are built from — the
        credential-with-no-record notifier#79 exists to prevent, in a smaller window.
        ``revoke`` locks the tenant row for the same shape of reason (CR 1).
        """
        statements = []

        @event.listens_for(db_session.sync_session, "do_orm_execute")
        def record(orm_context):
            statements.append(str(orm_context.statement).lower())

        await delete_tenant(db_session, committed, dry_run=False)

        event.remove(db_session.sync_session, "do_orm_execute", record)
        locks = [s for s in statements if "for update" in s and "tenants" in s]
        assert locks, statements
        assert statements.index(locks[0]) == 0, statements


class TestDryRun:
    async def test_writes_nothing(self, db_session, committed):
        await _furnish(db_session, committed)

        await delete_tenant(db_session, committed, dry_run=True)

        assert await _rows(db_session, Tenant, id=committed) == 1
        assert await _rows(db_session, ApiKey, tenant_id=committed) == 2

    async def test_still_names_what_would_go(self, db_session, committed):
        await _furnish(db_session, committed)

        inventory = await delete_tenant(db_session, committed, dry_run=True)

        assert [key.label for key in inventory.keys] == ["first", "second"]

    async def test_records_nothing(self, db_session, committed, caplog):
        """A rehearsal that leaves "api key destroyed with tenant" in the
        journal is a credential recorded dead that is still live — worse than
        no record, because someone will trust it."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        await _furnish(db_session, committed)
        caplog.clear()  # the setup's own mints are not what this asserts on

        await delete_tenant(db_session, committed, dry_run=True)

        assert _audit(caplog) == []


class TestAuditRecords:
    """The gap notifier#79 was filed for, carried over.

    notifier#67 put every mint and every revoke on the key-audit journal tag.
    A tenant delete bypassed all of it: the keys went by cascade, never
    through ``revoke()``, so nothing was emitted and the rows were gone. The
    first row of notifier#67's "predates the channel" table is exactly that case.
    """

    async def test_one_record_per_cascaded_key(self, db_session, committed, caplog):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        await _furnish(db_session, committed)
        caplog.clear()

        await delete_tenant(db_session, committed, dry_run=False)

        destroyed = [r for r in _audit(caplog) if r.message == "api key destroyed with tenant"]
        assert len(destroyed) == 2
        assert {r.label for r in destroyed} == {"first", "second"}
        assert {r.tenant_id for r in destroyed} == {committed}
        assert all(r.key_prefix.startswith("csk_") for r in destroyed)
        assert {r.environment for r in destroyed} == {"production", "development"}

    async def test_the_record_names_the_key_by_id(self, db_session, committed, caplog):
        """The row is gone; this id is the only handle anything will ever have
        on the credential that died."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        key, _ = await mint(db_session, committed, "only", "production")
        key_id = str(key.id)
        await db_session.commit()
        caplog.clear()

        await delete_tenant(db_session, committed, dry_run=False)

        (destroyed,) = [r for r in _audit(caplog) if r.message == "api key destroyed with tenant"]
        assert destroyed.key_id == key_id

    async def test_a_revoke_and_a_cascade_are_told_apart(self, db_session, committed, caplog):
        """ "api key revoked" is one credential retired on purpose; this is
        every credential a consumer held going at once. An operator reading
        the channel back needs to know which happened."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        await _furnish(db_session, committed)
        caplog.clear()

        await delete_tenant(db_session, committed, dry_run=False)

        assert "api key revoked" not in {r.message for r in _audit(caplog)}

    async def test_never_records_a_raw_key(self, db_session, committed, caplog):
        """None is held at this point — the rows carry only the digest — but
        the assertion belongs with the others (notifier#79)."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        _, raw = await mint(db_session, committed, "only", "production")
        await db_session.commit()
        caplog.clear()

        await delete_tenant(db_session, committed, dry_run=False)

        payload = json.dumps([r.__dict__ for r in _audit(caplog)], default=str)
        assert raw not in payload
        assert "key_hash" not in payload

    async def test_the_summary_closes_the_run(self, db_session, committed, caplog):
        """Last, not first. A process that dies part-way through emission
        leaves the per-key records with no summary after them, which is how
        an operator tells a partial record from a complete one."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        await _furnish(db_session, committed)
        caplog.clear()

        await delete_tenant(db_session, committed, dry_run=False)

        records = _audit(caplog)
        assert [r.message for r in records[:-1]] == ["api key destroyed with tenant"] * 2
        summary = records[-1]
        assert summary.message == "tenant deleted"
        assert summary.tenant_id == committed
        assert summary.tenant_name == "doomed-tenant"
        assert summary.keys_destroyed == 2

    async def test_a_keyless_tenant_still_leaves_a_record(self, db_session, committed, caplog):
        """No keys to name is not nothing to record: the tenant still went."""
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

        await delete_tenant(db_session, committed, dry_run=False)

        (summary,) = _audit(caplog)
        assert summary.message == "tenant deleted"
        assert summary.keys_destroyed == 0

    async def test_a_refused_delete_records_nothing(self, db_session, caplog):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)

        with pytest.raises(TenantNotFoundError):
            await delete_tenant(db_session, "01J0NOSUCHTENANT0000000000", dry_run=False)

        assert _audit(caplog) == []
