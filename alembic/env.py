"""Alembic migration environment — async PostgreSQL.

Online runs cross ``src.core.db_safety`` like every other connection (#15).
Production is migrated by ``scripts/deploy.sh``, which opts in for live only;
a hand-run ``alembic upgrade head`` against it is refused. Offline (``--sql``)
runs are exempt: they print SQL and connect to nothing.
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from src.core.db_safety import ProductionDatabaseError, assert_safe_database_url
from src.core.models import Base
from src.core.models.base import ULIDType

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render ULIDType as sa.String(26) in migrations."""
    if type_ == "type" and isinstance(obj, ULIDType):
        return "sa.String(length=26)"
    return False


def get_url() -> str:
    """Read database URL from environment or alembic.ini."""
    return os.environ.get(
        "DATABASE_URL",
        config.get_main_option("sqlalchemy.url", ""),
    )


def guarded_url() -> str:
    """The URL to connect to, refused if production without the opt-in.

    The guard's own advice is to set ``STATUS_ALLOW_PROD_DB=1``, which is
    wrong for a migration: that flag belongs to ``deploy.sh``. So the refusal
    is re-raised naming the deploy, with the guard's reason chained above it.
    """
    url = get_url()
    try:
        assert_safe_database_url(url)
    except ProductionDatabaseError as exc:
        raise ProductionDatabaseError(
            "alembic will not open production by hand: scripts/deploy.sh migrates "
            "it, after dev (docs/DEPLOYMENT.md). For dev, run the same command "
            'with DATABASE_URL="$DEV_DATABASE_URL".'
        ) from exc
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without connecting."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations using a sync connection."""
    context.configure(
        connection=connection, target_metadata=target_metadata, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode — async connection."""
    connectable = create_async_engine(guarded_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
