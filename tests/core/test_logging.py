"""Regression tests: JSON log records carry timestamp, level, and logger name,
and uvicorn's own loggers share the app's JSON formatter and drop uvicorn's
ANSI `color_message` extra (issues notifier#11, notifier#14; skills#69, skills#81, skills#82).
"""

import json
import logging
import logging.config
import os
import subprocess
import sys
from pathlib import Path

from src.core.logging import (
    AUDIT_IDENT,
    AUDIT_LOGGER_NAME,
    AUDIT_SOCKET_ENV,
    ColorMessageFilter,
    build_json_formatter,
    configure_audit_logging,
    configure_logging,
    configure_script_logging,
    get_audit_logger,
    get_logger,
)

LOG_CONFIG_PATH = Path("src/core/log_config.json")


def _snapshot(names):
    """Capture mutable logger state so a dictConfig() call can be rolled back.

    dictConfig() and configure_logging() both mutate live loggers; leaking that
    into later tests is an order-dependent flake, so every attribute they touch
    is saved and restored.
    """
    saved = {}
    for name in names:
        logger = logging.getLogger(name)
        saved[name] = {
            "handlers": logger.handlers[:],
            "filters": logger.filters[:],
            "propagate": logger.propagate,
            "level": logger.level,
        }
    return saved


def _restore(saved):
    for name, attrs in saved.items():
        logger = logging.getLogger(name)
        for attr, value in attrs.items():
            setattr(logger, attr, value)


def test_log_record_includes_structured_fields(capsys):
    """Regression: bare JsonFormatter() emitted only {"message"} (issue notifier#11)."""
    saved = _snapshot(("",))  # "" is the root logger — configure_logging() rebinds it
    try:
        configure_logging()
        get_logger("src.some.module").warning("hello %s", "world")
    finally:
        _restore(saved)

    record = json.loads(capsys.readouterr().out)
    assert record["message"] == "hello world"
    assert record["level"] == "WARNING"
    assert record["logger"] == "src.some.module"
    assert "timestamp" in record


def test_uvicorn_log_config_is_valid_and_shares_formatter():
    """The uvicorn --log-config file wires uvicorn's loggers through the same
    formatter as the app, and dictConfig accepts it (a malformed file would
    fail the service at boot, not in review)."""
    config = json.loads(LOG_CONFIG_PATH.read_text())

    # Single source of truth: the file builds its formatter from the factory
    # configure_logging() also uses, not a duplicated fmt string.
    assert any(
        f.get("()") == "src.core.logging.build_json_formatter"
        for f in config["formatters"].values()
    )
    # All three uvicorn loggers must be present, else they keep the plain
    # default — and each must carry the color_message strip. Asserting
    # placement, not just effect: moving the filter to the stdout handler still
    # produces clean JSON today, and that is the variant that breaks silently
    # under a sink reading record.__dict__ directly (skills#82).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert name in config["loggers"]
        assert "strip_color_message" in config["loggers"][name]["filters"]

    saved = _snapshot(("", "uvicorn", "uvicorn.error", "uvicorn.access"))
    try:
        logging.config.dictConfig(config)  # raises on a malformed config
    finally:
        _restore(saved)


def test_shared_formatter_renders_uvicorn_access_record():
    """A uvicorn.access record formats to JSON with the same fields as app logs
    — the request line lands in `message`, not a plain-text handler."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:0", "GET", "/health", "1.1", 200),
        exc_info=None,
    )
    parsed = json.loads(build_json_formatter().format(record))
    assert parsed["logger"] == "uvicorn.access"
    assert parsed["level"] == "INFO"
    assert parsed["message"] == '127.0.0.1:0 - "GET /health HTTP/1.1" 200'
    assert "timestamp" in parsed


def test_color_message_filter_strips_extra_at_the_record_source():
    """uvicorn's ANSI-duplicate `color_message` extra never reaches a payload.

    Asserts on the record itself, not only the rendered JSON: the strip has to
    hold for any sink, including handlers that read `record.__dict__` directly
    rather than going through a `logging.Formatter` (skills#82).
    """
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Started server process [%d]",
        args=(4066888,),
        exc_info=None,
    )
    record.color_message = "Started server process [\x1b[36m%d\x1b[0m]"

    assert ColorMessageFilter().filter(record) is True  # never drops a record
    assert not hasattr(record, "color_message")

    parsed = json.loads(build_json_formatter().format(record))
    assert "color_message" not in parsed
    assert parsed["message"] == "Started server process [4066888]"


class TestAuditChannel:
    """The mint/revoke audit channel (notifier#67).

    `src/core/api_keys.py` has logged a record on every mint and every revoke
    since notifier#62, and not one had ever been emitted: neither credential script
    called `configure_logging()`, so the root logger had no handler and
    Python's last-resort handler dropped every INFO record. The trade notifier#62 made
    — DELETE over a `revoked_at` column, paid for by a log line — was being
    paid with a line that did not exist.
    """

    def test_a_record_reaches_the_syslog_socket(self, audit_socket):
        """The round trip, not the wiring: bytes leave the process."""
        saved = _snapshot(("", AUDIT_LOGGER_NAME))
        try:
            configure_audit_logging(address=audit_socket.path)
            get_audit_logger().info("api key revoked", extra={"key_id": "01J0KEY"})
        finally:
            _restore(saved)

        record = audit_socket.records()[0]
        assert record["message"] == "api key revoked"
        assert record["key_id"] == "01J0KEY"
        assert record["level"] == "INFO"
        assert "timestamp" in record

    def test_the_datagram_carries_the_journalctl_tag(self, audit_socket):
        """`journalctl -t status-keys` is the read-back, and journald files
        a record under that tag by parsing the syslog `ident: ` prefix. Without
        it the records land in the journal untagged, which is a durable record
        nobody can find."""
        saved = _snapshot(("", AUDIT_LOGGER_NAME))
        try:
            configure_audit_logging(address=audit_socket.path)
            get_audit_logger().info("api key minted")
        finally:
            _restore(saved)

        assert audit_socket.datagrams()[0].startswith(f"<14>{AUDIT_IDENT}: {{")

    def test_records_never_reach_stdout(self, audit_socket, capsys):
        """stdout is the operator's channel: `raw_key=` lines an operator pastes
        into a consumer's secrets, and the two lines `clients/python`'s fixture
        parses. Interleaving JSON there is the trap notifier#67 names in the obvious
        fix."""
        saved = _snapshot(("", AUDIT_LOGGER_NAME))
        try:
            configure_logging()  # binds the root logger to stdout
            configure_audit_logging(address=audit_socket.path)
            get_audit_logger().info("api key minted")
        finally:
            _restore(saved)

        assert capsys.readouterr().out == ""
        assert audit_socket.records()[0]["message"] == "api key minted"

    def test_an_unreachable_socket_degrades_loudly_to_stderr(self, tmp_path, capsys):
        """The one failure this must never repeat is a silent one. A host with
        no journal socket gets the records on stderr and a warning saying they
        are not durable — never nothing."""
        saved = _snapshot(("", AUDIT_LOGGER_NAME))
        try:
            configure_audit_logging(address=str(tmp_path / "absent.sock"))
            get_audit_logger().info("api key revoked", extra={"key_id": "01J0KEY"})
        finally:
            _restore(saved)

        captured = capsys.readouterr()
        assert captured.out == ""
        warning, record = (json.loads(line) for line in captured.err.strip().splitlines())
        assert warning["level"] == "WARNING"
        assert "absent.sock" in warning["audit_socket"]
        assert record["message"] == "api key revoked"
        assert record["key_id"] == "01J0KEY"

    def test_reconfiguring_closes_the_handler_it_replaces(self, audit_socket):
        """Assigning over `handlers` drops the reference without closing the
        socket under it, which is an fd leak anywhere this is called twice
        (CR 5)."""
        saved = _snapshot(("", AUDIT_LOGGER_NAME))
        try:
            first = configure_audit_logging(address=audit_socket.path)
            configure_audit_logging(address=audit_socket.path)
        finally:
            _restore(saved)

        # SysLogHandler.close() closes the socket and drops the attribute to
        # None, so `is None` is the durable spelling of "it was closed".
        assert first.socket is None

    def test_the_environment_variable_selects_the_socket(self, audit_socket, monkeypatch):
        """How a test — and only a test — points the channel somewhere else."""
        monkeypatch.setenv(AUDIT_SOCKET_ENV, audit_socket.path)
        saved = _snapshot(("", AUDIT_LOGGER_NAME))
        try:
            configure_audit_logging()
            get_audit_logger().info("api key minted")
        finally:
            _restore(saved)

        assert audit_socket.records()[0]["message"] == "api key minted"


class TestConfigureScriptLogging:
    """What the two credential scripts call at their entry point."""

    def test_it_keeps_stdout_for_the_operator_and_journals_the_audit(
        self, audit_socket, monkeypatch, capsys
    ):
        monkeypatch.setenv(AUDIT_SOCKET_ENV, audit_socket.path)
        saved = _snapshot(("", AUDIT_LOGGER_NAME))
        try:
            configure_script_logging()
            get_logger("src.core.database").info("connected")
            get_audit_logger().info("api key minted")
        finally:
            _restore(saved)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert json.loads(captured.err)["message"] == "connected"
        assert audit_socket.records()[0]["message"] == "api key minted"


class TestEveryEntryPointConfiguresTheChannel:
    """notifier#67, CR 4. The channel is configured per entry point, so an entry point
    that skips it is exactly the defect notifier#67 was: `journalctl -t status-keys`
    is now documented as *the* answer to which key was minted or revoked, and
    that claim is only true if every process able to mint is on the channel.

    The API mints nothing today — `src/api/deps.py` imports `hash_key` alone —
    so this asserts the wiring rather than a record. A route that mints would
    otherwise land its record untagged in `status.service`'s own journal, and
    the documented command would miss it silently.
    """

    def test_the_api_entry_point_opens_the_audit_channel(self, audit_socket):
        """A subprocess, not `importlib.reload`: reloading `src.api.main`
        rebuilds its module-level `app`, and the `client` fixture's
        `dependency_overrides` are then keyed against an object no later test
        holds — a green assertion here for a failure two files away."""
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import src.api.main\n"
                "from src.core.logging import get_audit_logger\n"
                "get_audit_logger().info('api key minted')\n",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, AUDIT_SOCKET_ENV: audit_socket.path},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr

        assert audit_socket.records()[0]["message"] == "api key minted"
        assert "api key minted" not in done.stdout
