"""Tenant model — the isolation boundary between co-status consumers."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.base import Base, TimestampMixin, ULIDType, generate_ulid


class Tenant(Base, TimestampMixin):
    """A consumer of co-status. It owns its API keys and, from Phase 2, its monitors."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(ULIDType, primary_key=True, default=generate_ulid)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
