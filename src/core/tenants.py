"""Delete a tenant, and record every credential the cascade takes with it.

``src/core/api_keys.py`` is the one spelling of how a credential is made and
destroyed, and every mint and revoke lands on ``journalctl -t status-keys``
(notifier#67). Deleting a tenant would bypass all of it: keys go by
``ON DELETE CASCADE``, never through :func:`~src.core.api_keys.revoke`, so no
record would be emitted and the rows would be gone. A revoke retires one
credential; this retires every credential a consumer holds (notifier#79).

**Unlike ``mint`` and ``revoke``, this owns its transaction.** Those two flush
and leave the commit to the caller, which is what puts both halves of a
rotation in one. Here the audit records must follow the commit: a rehearsal
that leaves "api key destroyed with tenant" in the journal is a live
credential recorded dead, which is worse than no record at all because someone
will trust it. So ``dry_run`` rolls back and stays silent, and a real run
commits and then records — inseparably, in one function, so there is no way to
perform the deletion through this module and skip the record.

**The tenant row is locked ``FOR UPDATE`` before anything is read.** The
inventory and the delete are separate statements, and a key minted in the gap
between them would be destroyed by the cascade while being absent from the
snapshot the records are built from — the credential-with-no-record this
module exists to prevent, in a smaller window. :func:`delete_tenant` carries
the mechanism.
"""

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.api_keys import KeyRecord, TenantNotFoundError, keys_for, ulid_str
from src.core.logging import get_audit_logger
from src.core.models import Tenant

#: The same channel every mint and revoke lands on. A cascaded key is a key
#: destroyed, and an operator asking "which credential died, and when" must
#: get one answer, not two places to look (notifier#67, notifier#79).
audit = get_audit_logger()


@dataclass(frozen=True, slots=True)
class TenantInventory:
    """What a tenant owns, captured before any of it goes.

    Each key is named individually on the audit channel, because a key is a
    credential someone may still be holding.

    A snapshot for the same reason :class:`~src.core.api_keys.KeyRecord` is
    one: after the DELETE, reading any of this off the ORM is a refresh
    against rows that no longer exist.
    """

    tenant_id: str
    tenant_name: str
    keys: list[KeyRecord]


async def inventory_of(session: AsyncSession, tenant_id: str) -> TenantInventory:
    """Return what *tenant_id* owns. Reads only; writes nothing.

    Raises :class:`~src.core.api_keys.TenantNotFoundError` for an id that does
    not exist, rather than returning an inventory of zeroes. To an operator
    about to delete, "this tenant owns nothing" and "you mistyped the ULID"
    are opposite facts that would otherwise print identically — and the second
    one, followed through, deletes nothing and reports success.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFoundError(f"no tenant with id {ulid_str(tenant_id)}")

    return TenantInventory(
        tenant_id=ulid_str(tenant_id),
        tenant_name=tenant.name,
        keys=await keys_for(session, tenant_id),
    )


async def delete_tenant(
    session: AsyncSession,
    tenant_id: str,
    *,
    dry_run: bool = True,
) -> TenantInventory:
    """Delete *tenant_id* and everything it owns; return what went.

    The inventory is read first, while the rows are still there to be read —
    it is both what the caller prints and what the audit records are built
    from. Then the tenant goes, and the cascade takes its keys.

    ``dry_run`` rolls the whole thing back and emits nothing, and is the
    default so a caller that forgets the flag writes nothing — the same
    posture as ``scripts/rotate_key.py:apply``. Every refusal still fires
    under it: a rehearsal that passes where the real run would fail certifies
    the wrong thing.

    Commits on a real run, then records. The records come last because a
    record of a deletion that then failed to commit is a credential reported
    dead while it still authenticates.

    The tenant row is locked ``FOR UPDATE`` before the inventory is read, for
    the reason :func:`~src.core.api_keys.revoke` locks it before counting:
    otherwise the read and the delete are two statements with a gap between
    them, and a key minted in that gap is destroyed by the cascade while being
    absent from the snapshot the records are built from — a credential gone
    with nothing naming it, which is the whole of what notifier#79 closes. The lock
    reaches a concurrent :func:`~src.core.api_keys.mint` because inserting a
    row that references this tenant takes ``FOR KEY SHARE`` on it, and that
    conflicts: the mint blocks here and then fails on a parent that is gone.
    """
    tenant_id = ulid_str(tenant_id)
    try:
        await session.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        inventory = await inventory_of(session, tenant_id)
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.flush()
    except Exception:
        await session.rollback()
        raise

    if dry_run:
        await session.rollback()
        return inventory

    await session.commit()

    for key in inventory.keys:
        audit.info(
            "api key destroyed with tenant",
            extra={
                "tenant_id": inventory.tenant_id,
                "key_id": key.id,
                "key_prefix": key.key_prefix,
                "label": key.label,
                "environment": key.environment,
            },
        )
    # Last, and emitted even for a tenant that held no keys. It is what says
    # the run finished: per-key records with no summary after them are a
    # process that died part-way through recording, and that difference is
    # only legible if the summary is always the closing line.
    audit.info(
        "tenant deleted",
        extra={
            "tenant_id": inventory.tenant_id,
            "tenant_name": inventory.tenant_name,
            "keys_destroyed": len(inventory.keys),
        },
    )
    return inventory
