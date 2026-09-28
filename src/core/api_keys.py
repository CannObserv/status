"""Mint and revoke tenant API keys.

The one spelling of how a credential is made, hashed, and destroyed. Three
callers share it: ``src/api/deps.py`` verifies against :func:`hash_key`,
``scripts/seed_tenant.py`` mints a new tenant's first key, and
``scripts/rotate_key.py`` attaches, revokes, and rotates keys on an existing
tenant.

Before notifier#62 the minting half lived only inside ``seed_tenant.py``. Two
consequences, both fixed by moving it here:

* ``[tool.coverage.run] source = ["src"]`` does not reach ``scripts/``, so
  credential logic was the one part of this service the ``fail_under = 80``
  gate never measured.
* The SHA-256 was spelled twice — once where keys are made, once where they
  are checked. Two spellings of a hash is a credential that mints fine and
  cannot authenticate, discovered by a consumer rather than by a test.

**Neither :func:`mint` nor :func:`revoke` commits.** They flush, and the
caller owns the transaction. That is what lets a rotation put the mint and the
revoke in one, so an interrupted rotation cannot leave a tenant with no
working key.

**Revocation is a DELETE, deliberately.** A ``revoked_at`` column would leave
an audit trail, but it would also require every present and future key lookup
to filter on it — and forgetting that filter anywhere means a revoked
credential that still authenticates. DELETE fails closed; a column that must
be remembered fails open. The audit value is taken here instead, as a log line
on every mint and revoke that names the key without ever naming its secret.

That line goes to the ``status.audit`` logger, which the credential scripts
point at journald — ``journalctl -t status-keys``. It went to this module's
own logger until notifier#67, where it turned out never to have been emitted at all:
neither script called ``configure_logging()``, so the root logger had no
handler and Python's last-resort handler dropped every INFO record. The half
of the trade above that was supposed to pay for the missing column had not
run once.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.logging import get_audit_logger, get_logger
from src.core.models import ApiKey, Tenant

logger = get_logger(__name__)

#: Every mint and every revoke, on the channel an entry point points at the
#: journal. Not ``logger``: these records are the whole audit trail this
#: module's DELETE leaves behind, and they have to reach somewhere durable
#: even when the module's other output goes to an operator's terminal (notifier#67).
audit = get_audit_logger()

#: Marks a co-status credential on sight, in a config file or a leak report, and
#: keeps it visibly distinct from notifier's ``nk_`` keys.
RAW_KEY_PREFIX = "csk_"

#: Stored alongside the hash so an operator can tell two keys apart without
#: holding either. Long enough to be unambiguous, short enough to be useless.
KEY_PREFIX_LENGTH = 8

#: The deployments a key may be marked for. A production deployment refuses a
#: ``development`` key at the auth layer (notifier#22, finding 3); the model carries
#: the same pair as a CheckConstraint.
ENVIRONMENTS = ("production", "development")


class TenantNotFoundError(LookupError):
    """Raised when the named tenant does not exist."""


class KeyNotFoundError(LookupError):
    """Raised when the named API key does not exist."""


class KeyOwnershipError(LookupError):
    """Raised when the named API key belongs to a different tenant."""


class LastKeyError(RuntimeError):
    """Raised when revoking would leave a tenant with no keys at all."""


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """What a revoked key was, captured before the row went away.

    A snapshot rather than the ORM object: after the DELETE, reading an
    attribute off the instance is a refresh against a row that no longer
    exists. The operator still needs to see what just died — which is exactly
    the confirmation the ad-hoc rotation in notifier#57 had no way to print.
    """

    id: str
    tenant_id: str
    label: str
    key_prefix: str
    environment: str
    created_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def of(cls, key: ApiKey) -> "KeyRecord":
        """Snapshot *key* while its row is still there to be read."""
        return cls(
            id=str(key.id),
            tenant_id=str(key.tenant_id),
            label=key.label,
            key_prefix=key.key_prefix,
            environment=key.environment,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
        )


def ulid_str(value: object) -> str:
    """Render a ULID column value or a CLI argument as the same string.

    ``ULIDType`` hands back a :class:`ulid.ULID` for a row read from the
    database, while a freshly constructed instance holds whatever was assigned
    and an operator's argv holds a ``str``. The two do compare equal today,
    but a tenancy check that leans on that is one library release away from
    refusing every revoke — or, inverted, from allowing the one that matters.
    Compare and report the rendered form, which is also what an operator
    wants to read back: ``01J…``, not ``ULID(01J…)``.
    """
    return str(value)


def hash_key(raw: str) -> str:
    """Return the digest a raw key is stored and looked up by."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_raw_key() -> str:
    """Return a fresh raw API key. The only place one is ever created."""
    return RAW_KEY_PREFIX + secrets.token_urlsafe(32)


async def key_count(session: AsyncSession, tenant_id: str) -> int:
    """Return how many API keys *tenant_id* currently holds."""
    result = await session.execute(
        select(func.count()).select_from(ApiKey).where(ApiKey.tenant_id == tenant_id)
    )
    return int(result.scalar_one())


async def keys_for(session: AsyncSession, tenant_id: str) -> list[KeyRecord]:
    """Return *tenant_id*'s keys, oldest first, as snapshots.

    Revoking takes a key id, and until this existed there was no sanctioned
    way to read one — which sends an operator back to the ad-hoc SQL notifier#62 is
    trying to retire. Snapshots rather than rows, because nothing downstream
    should be able to mutate a key by accident while reading the list.

    Raises :class:`TenantNotFoundError` rather than returning an empty list
    for an id that does not exist: to an operator about to rotate, "no keys"
    and "no such tenant" are the difference between a consumer that is already
    down and a typo in the argument they just pasted.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFoundError(f"no tenant with id {ulid_str(tenant_id)}")
    result = await session.execute(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at, ApiKey.id)
    )
    return [KeyRecord.of(key) for key in result.scalars().all()]


async def mint(
    session: AsyncSession,
    tenant_id: str,
    label: str,
    environment: str,
) -> tuple[ApiKey, str]:
    """Create an API key for *tenant_id*; return the row and the raw key.

    The raw key is returned once and never persisted — only its digest is.
    Flushes so a caller can count keys or revoke in the same transaction;
    does not commit.

    Raises :class:`ValueError` for an unknown *environment* and
    :class:`TenantNotFoundError` for an unknown *tenant_id*. Both would
    otherwise surface at flush as a constraint or foreign-key error naming a
    database object rather than the argument the operator typed.
    """
    if environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS}, got {environment!r}")
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFoundError(f"no tenant with id {ulid_str(tenant_id)}")

    raw = generate_raw_key()
    key = ApiKey(
        tenant_id=tenant_id,
        label=label,
        key_prefix=raw[:KEY_PREFIX_LENGTH],
        key_hash=hash_key(raw),
        environment=environment,
    )
    session.add(key)
    await session.flush()
    audit.info(
        "api key minted",
        extra={
            "tenant_id": ulid_str(tenant_id),
            "key_id": ulid_str(key.id),
            "key_prefix": key.key_prefix,
            "label": label,
            "environment": environment,
        },
    )
    return key, raw


async def revoke(
    session: AsyncSession,
    tenant_id: str,
    key_id: str,
    *,
    allow_last: bool = False,
) -> KeyRecord:
    """Delete *key_id* from *tenant_id*; return a snapshot of what went.

    Scoped by tenant *and* key. Naming the key alone would let one mistyped
    ULID revoke a different consumer's credential — the failure the operator
    would discover from that consumer's outage rather than from this call.

    Refuses to remove a tenant's only key unless *allow_last*: a tenant with
    zero keys is a consumer that is down, and the sweep will mark its monitor
    missing without saying why. The guard does not fire on a rotation, because
    :func:`mint` has already flushed the replacement by then.

    The tenant row is locked ``FOR UPDATE`` before the count is read. Without
    it the check is a read and the delete a later write, so two revokes racing
    on one tenant could each observe two keys and each delete one — landing
    the tenant at exactly the zero the guard exists to prevent. Unlikely for a
    hand-run script, and two operators during an incident is what this is for.

    Flushes; does not commit.
    """
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise KeyNotFoundError(f"no api key with id {ulid_str(key_id)}")
    owner = ulid_str(key.tenant_id)
    if owner != ulid_str(tenant_id):
        raise KeyOwnershipError(
            f"api key {ulid_str(key_id)} belongs to tenant {owner}, not {ulid_str(tenant_id)}"
        )
    if not allow_last:
        # Lock inside the branch that reads the count: under allow_last the
        # delete is unconditional and there is no read-then-write to
        # serialize, and a lock that protects nothing invites the next reader
        # to assume it protects something (CR 16).
        await session.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        if await key_count(session, tenant_id) <= 1:
            raise LastKeyError(
                f"api key {ulid_str(key_id)} is the last key tenant "
                f"{ulid_str(tenant_id)} holds; "
                f"revoking it leaves that consumer unable to authenticate at all"
            )

    record = KeyRecord.of(key)
    await session.delete(key)
    await session.flush()
    audit.info(
        "api key revoked",
        extra={
            "tenant_id": ulid_str(tenant_id),
            "key_id": record.id,
            "key_prefix": record.key_prefix,
            "label": record.label,
            "environment": record.environment,
        },
    )
    return record
