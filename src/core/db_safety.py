"""Refuse to open a non-production process against the production database.

The documented dev-server recipe sources ``/etc/status/.env``, which sets
``DATABASE_URL`` to production. A hand-run uvicorn on port 9001 therefore
shares one database with the live service on 9000 (see issue notifier#21/#22, root
incident CannObserv/archiver#98).

This module is the single chokepoint: :func:`assert_safe_database_url` is
called from :func:`src.core.database.get_database_url`, which every path that
opens a connection crosses — the FastAPI app, ``scripts/seed_tenant.py``, and
anything added later.

Two deliberate carve-outs:

* **Alembic is exempt.** ``alembic/env.py`` reads ``os.environ`` directly and
  never calls this. That is intended: this host is both development and
  production — ``main`` *is* the deployed code — so ``alembic upgrade head``
  against production is the correct operation, not the bug. co-status keeps
  notifier's arrangement: one VM serving both, which is what makes this
  carve-out necessary.
* **The escape flag lives in the systemd unit, never an EnvironmentFile.**
  ``deploy/status.service`` carries ``EnvironmentFile=`` lines for both
  ``/etc/status/.env`` and the repo ``.env``; a flag placed in either would
  be inherited by every process that sources them, re-opening the hole for
  exactly the hand-run servers this guard targets.
"""

import os
from urllib.parse import urlsplit

ALLOW_PROD_ENV_VAR = "STATUS_ALLOW_PROD_DB"

#: A database is treated as non-production only when its name *ends* with one
#: of these. Suffix, not substring: ``test_status`` and ``status_testing``
#: are production names that a substring check would wrongly wave through.
NON_PRODUCTION_SUFFIXES = ("_test", "_dev")


class ProductionDatabaseError(RuntimeError):
    """Raised when a process would open the production database unguarded."""


def database_name(url: str) -> str:
    """Return the database name from a SQLAlchemy or libpq URL.

    Compares the *name*, not the URL string: ``postgresql://…/status`` and
    ``postgresql+asyncpg://…/status`` are different strings naming one
    database, so string equality is bypassable.

    Fails closed — raises :class:`ValueError` on any URL it cannot read,
    rather than returning a name the caller might treat as non-production.
    """
    if "://" not in url or not urlsplit(url).scheme:
        raise ValueError(f"no scheme — expected postgresql+asyncpg://HOST/NAME, got {_redact(url)}")
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    if not name or "/" in name:
        raise ValueError(f"database URL has no database name: {_redact(url)}")
    return name


def is_non_production(name: str) -> bool:
    """True when the database name carries a ``_test`` or ``_dev`` suffix."""
    return name.endswith(NON_PRODUCTION_SUFFIXES)


def environment_label(name: str) -> str:
    """Classify a database name in the API-key vocabulary.

    One spelling of the rule, because both health probes apply it and their
    *disagreement* is a documented signal: ``/health`` classifies the
    configured URL, ``/ready`` the database actually connected, and
    DEPLOYMENT.md reads a mismatch as those two having diverged. A second
    spelling would give the pair a way to disagree that means nothing.

    Takes a name rather than reading the environment, so a caller that has
    already resolved one — from a URL or from ``current_database()`` — does
    not parse it twice.
    """
    return "development" if is_non_production(name) else "production"


def serving_production() -> bool:
    """True when this process is serving the production database.

    Fails safe: an unset or unparseable ``DATABASE_URL`` is treated as
    production, so a misconfigured process refuses development credentials
    rather than accepting them. Deliberately ignores
    ``STATUS_ALLOW_PROD_DB`` — that flag permits *opening* production, it
    does not reclassify a dev database as one.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return True
    try:
        return not is_non_production(database_name(url))
    except ValueError:
        return True


def assert_safe_database_url(url: str) -> None:
    """Raise :class:`ProductionDatabaseError` unless *url* is safe to open.

    Safe means either the database name is non-production, or the operator
    has explicitly opted in by setting ``STATUS_ALLOW_PROD_DB=1`` (exactly
    ``1``; a stray truthy string does not count).
    """
    if os.environ.get(ALLOW_PROD_ENV_VAR) == "1":
        return
    try:
        name = database_name(url)
    except ValueError as exc:
        raise ProductionDatabaseError(
            f"Refusing to open DATABASE_URL: {exc}. A URL this guard cannot "
            f"parse is treated as production. Set {ALLOW_PROD_ENV_VAR}=1 only "
            f"if this is a deliberate production operation."
        ) from exc
    if is_non_production(name):
        return
    raise ProductionDatabaseError(
        f"Refusing to open production database {name!r}. Expected a name "
        f"ending in {' or '.join(NON_PRODUCTION_SUFFIXES)}. "
        f"Use scripts/dev_server.sh for a dev server, or set "
        f"{ALLOW_PROD_ENV_VAR}=1 for a deliberate production operation "
        f"(in the systemd unit — never in an env file)."
    )


def _redact(url: str) -> str:
    """Strip anything before an ``@`` so credentials never reach a log."""
    return url.rsplit("@", 1)[-1]


def main() -> int:
    """Check ``DATABASE_URL`` from the environment. Exit 0 if safe, 1 if not.

    ``scripts/dev_server.sh`` calls this rather than reimplementing the URL
    parsing in bash, so the two can never diverge.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("db_safety: DATABASE_URL is not set", flush=True)
        return 1
    try:
        assert_safe_database_url(url)
    except ProductionDatabaseError as exc:
        print(f"db_safety: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
