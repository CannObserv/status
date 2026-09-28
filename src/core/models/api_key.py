"""ApiKey model — hashed API credentials owned by a Tenant."""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base, ULIDType, generate_ulid


class ApiKey(Base):
    """Stores a SHA-256 hash of each API key; raw key is never persisted.

    ``environment`` marks which deployment a key is meant for. A production
    deployment refuses ``development`` keys outright — the layer that catches
    a sibling's dev process calling production over HTTP, which no database
    guard can see (issue notifier#22, finding 3).
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('production', 'development')",
            name="ck_api_keys_environment",
        ),
    )

    id: Mapped[str] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    tenant_id: Mapped[str] = mapped_column(
        ULIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    environment: Mapped[str] = mapped_column(
        String, nullable=False, default="production", server_default="production"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
