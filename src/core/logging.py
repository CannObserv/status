"""Structured JSON logging utilities."""

import logging
import logging.handlers
import os
import socket
import sys
from typing import TextIO

from pythonjsonlogger.json import JsonFormatter

#: The credential audit channel: every mint and every revoke, naming the key
#: and never its secret. Its own logger rather than ``src.core.api_keys``,
#: because the channel is the point — these records go somewhere durable while
#: everything else the module might ever log goes wherever the entry point
#: sends it.
AUDIT_LOGGER_NAME = "status.audit"

#: journald parses a syslog datagram's ``tag: `` prefix into
#: ``SYSLOG_IDENTIFIER``, which is what ``journalctl -t`` matches. Without it
#: the records still reach the journal, untagged — durable and unfindable.
AUDIT_IDENT = "status-keys"

#: journald's syslog datagram socket, and the audit channel's real
#: destination on this VM.
JOURNAL_SOCKET = "/dev/log"

#: Points the audit channel at another socket. For tests, which bind one and
#: read the records back; never set in production.
AUDIT_SOCKET_ENV = "STATUS_AUDIT_SOCKET"


def build_json_formatter() -> JsonFormatter:
    """The single JSON formatter definition for the whole process.

    Referenced by BOTH `configure_logging()` (non-uvicorn entry points) and
    `src/core/log_config.json` (uvicorn's `--log-config`, via the dictConfig
    `"()"` factory key), so app records and uvicorn's own access/error lines
    serialize with one identical schema — no drift, one place to change.

    Keys must be named in the fmt: a bare JsonFormatter() defaults to
    "%(message)s" and emits records with no level, logger, or timestamp
    (issue notifier#11).
    """
    return JsonFormatter(
        "%(levelname)s %(name)s %(message)s",
        timestamp=True,
        rename_fields={"levelname": "level", "name": "logger"},
    )


class ColorMessageFilter(logging.Filter):
    """Drop uvicorn's `color_message` extra before anything serializes it.

    uvicorn logs its lifecycle lines with an ANSI-coloured duplicate of the
    message attached as `extra={"color_message": ...}`, for its own
    colour-aware default formatter. Every extra reaches the JSON payload, so
    without this the records carry a second copy of the message full of escape
    sequences — the one thing structured logging exists to avoid.

    A *filter*, not the formatter's `reserved_attrs`, and on the *loggers*
    rather than the handler: both choices put the strip at the record's source,
    before any handler reads it. A handler that builds its payload from the
    record's `__dict__` instead of a `logging.Formatter` would otherwise
    resurrect the field the day the sink changes, silently and with no failing
    test.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Strip the extra if present. Never drops a record."""
        record.__dict__.pop("color_message", None)
        return True


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Configure the root logger with JSON formatting. Call once at entry
    points that do NOT run under uvicorn (CLI scripts, alembic env, cron
    oneshots, tests). Under uvicorn, `--log-config src/core/log_config.json`
    configures the whole logging tree at boot instead; this call is then a
    harmless no-op-equivalent (it reinstalls an identical root handler), which
    keeps app logs JSON even if someone launches uvicorn without --log-config.

    *stream* is resolved at call time rather than bound as a default, so the
    handler picks up whatever ``sys.stdout`` is when the entry point runs —
    pytest's ``capsys`` among them. An entry point whose stdout is an
    interface — the credential scripts print lines an operator pastes and
    ``clients/python``'s fixture parses — passes ``sys.stderr`` instead, via
    :func:`configure_script_logging`.
    """
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(build_json_formatter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]


def get_audit_logger() -> logging.Logger:
    """Return the credential audit logger. Configured by the entry point."""
    return logging.getLogger(AUDIT_LOGGER_NAME)


def _datagram_socket_error(address: str) -> OSError | None:
    """Return why *address* cannot take a datagram, or ``None`` if it can.

    The check has to be ours. ``SysLogHandler.createSocket()`` deliberately
    swallows an unreachable socket — "syslog server may be unavailable during
    handler initialisation" — so constructing one against a path that does not
    exist succeeds, and the records then vanish at ``emit`` time through
    ``handleError``. Which is precisely the silent drop notifier#67 exists to end,
    reintroduced one layer down.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
            probe.connect(address)
    except OSError as exc:
        return exc
    return None


def configure_audit_logging(
    address: str | None = None, level: int = logging.INFO
) -> logging.Handler:
    """Send audit records to the journal, off the root logger's stream.

    ``propagate = False`` is the load-bearing half. These records must not
    reach whatever handler the entry point put on the root logger: on the two
    credential scripts that is stdout's neighbour, and stdout carries the raw
    key an operator is copying and the ``tenant_id=``/``raw_key=`` lines
    ``clients/python/tests/conftest.py`` parses. The audit record and the
    operator output want different channels (notifier#67).

    Returns the installed handler so a caller can assert on it.

    **A socket that cannot be reached degrades to stderr, loudly.** Dropping
    an audit record silently is the entire defect notifier#67 was filed for; a
    credential script on a host with no journal is a lesser problem than one
    that mints in silence, so the records still appear and a WARNING says they
    will not outlive the session.
    """
    address = address or os.environ.get(AUDIT_SOCKET_ENV) or JOURNAL_SOCKET
    audit = get_audit_logger()
    audit.setLevel(level)
    audit.propagate = False
    # Close what is being replaced. Assigning over `handlers` drops the
    # reference without closing the socket underneath it, so a second call
    # leaks an open fd — invisible while this is called once per process, and
    # a real leak the day anything reconfigures on a signal (CR 5).
    for existing in audit.handlers:
        existing.close()

    handler: logging.Handler
    unreachable = _datagram_socket_error(address)
    if unreachable is not None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(build_json_formatter())
        audit.handlers = [handler]
        audit.warning(
            "audit channel unavailable; records are going to stderr and will "
            "not outlive this shell session",
            extra={"audit_socket": address, "error": str(unreachable)},
        )
        return handler

    # SOCK_DGRAM is pinned rather than left to SysLogHandler's DGRAM-then-STREAM
    # fallback, so the handler speaks whatever the probe above tested. A
    # stream-only /dev/log therefore degrades to stderr loudly instead of
    # connecting on a path nothing verified (CR 10).
    handler = logging.handlers.SysLogHandler(address=address, socktype=socket.SOCK_DGRAM)
    # The trailing ": " is not cosmetic: it is the syslog TAG delimiter
    # journald splits on to populate SYSLOG_IDENTIFIER.
    handler.ident = f"{AUDIT_IDENT}: "
    handler.setFormatter(build_json_formatter())
    audit.handlers = [handler]
    return handler


def configure_script_logging(level: int = logging.INFO) -> None:
    """Configure logging for a credential script. Call once, at the entry point.

    Three streams, on purpose. **stdout** stays the operator's: only the
    ``key=value`` lines the script prints itself. **stderr** takes everything
    logged, so a script whose stdout is piped still says what it did.
    **The journal** takes the audit records, which are the half of notifier#62's
    DELETE-over-``revoked_at`` trade that has to outlive the shell.
    """
    configure_logging(level=level, stream=sys.stderr)
    configure_audit_logging(level=level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Use in modules as: logger = get_logger(__name__)"""
    return logging.getLogger(name)
