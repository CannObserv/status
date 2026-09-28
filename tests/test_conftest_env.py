"""The test harness must pin DATABASE_URL at the test database, and the audit
channel away from the journal.

A pytest run in a shell that sourced /etc/status/.env would otherwise leave
DATABASE_URL pointing at production for any code that reads it directly
(issue notifier#22, finding 2; archiver hit this as CannObserv/archiver#157).
"""

import importlib
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

from src.core import db_safety
from src.core.logging import AUDIT_SOCKET_ENV, get_audit_logger


def test_conftest_pins_database_url_to_the_test_database():
    assert os.environ["DATABASE_URL"] == os.environ["TEST_DATABASE_URL"]


def test_pinned_database_url_passes_the_production_guard():
    db_safety.assert_safe_database_url(os.environ["DATABASE_URL"])


class TestTheSuiteNeverWritesToTheJournal:
    """notifier#84. `journalctl -t status-keys` is the answer to which production key
    was minted, revoked, or destroyed, and a test mint is indistinguishable
    from a real one there: `environment: production`, an `csk_` prefix, a
    tenant_id that only a database lookup reveals as fake. 170 had landed
    before anyone noticed.

    The leak was `src.api.main`, which opens the audit channel at import — on
    `/dev/log`, for the life of the pytest process — so every in-process mint
    after the first test that imported the app reached the real journal.
    """

    def test_conftest_points_the_channel_at_the_suite_sink(self, suite_audit_sink):
        assert os.environ[AUDIT_SOCKET_ENV] == suite_audit_sink.path

    def test_an_in_process_record_after_the_app_import_reaches_the_sink(self, suite_audit_sink):
        """The exact path that leaked: import the app, then emit on the channel
        in-process, as `mint()` does."""
        importlib.import_module("src.api.main")
        marker = secrets.token_hex(8)

        get_audit_logger().info("api key minted", extra={"key_id": marker})

        assert suite_audit_sink.wait_for(marker)["key_id"] == marker

    def test_a_subprocess_inherits_the_sink(self, suite_audit_sink):
        """A script run with the suite's environment and no `audit_socket` of
        its own — the other path the issue names — lands in the sink too."""
        marker = secrets.token_hex(8)
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "import src.api.main\n"
                "from src.core.logging import get_audit_logger\n"
                "get_audit_logger().info('api key minted', extra={'key_id': sys.argv[1]})\n",
                marker,
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr

        assert suite_audit_sink.wait_for(marker)["key_id"] == marker

    def test_the_sink_never_blocks_a_sender(self, suite_audit_sink):
        """An unread datagram socket blocks its sender within a few dozen
        records, which here would be a suite hung inside `SysLogHandler`."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.settimeout(5)
            sender.connect(suite_audit_sink.path)
            for n in range(1000):
                sender.send(f'<14>status-keys: {{"n": {n}}}'.encode())
