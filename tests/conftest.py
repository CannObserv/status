"""Shared test fixtures — database session, tenants and keys, audit sinks, script runner."""

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.api.deps import get_db_session
from src.core.logging import AUDIT_SOCKET_ENV
from src.core.models import ApiKey, Base, Tenant

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL environment variable is not set. Load env:  set -a; . ./.env; set +a"
    )

# Pin DATABASE_URL at the test database for the whole session, before any
# fixture runs. Without this, a pytest run in a shell that sourced
# /etc/status/.env leaves DATABASE_URL pointing at production for any code
# that reads it directly (issue notifier#22; archiver hit this as archiver#157).
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _audit_payload(datagram: str) -> dict:
    """Strip the syslog `<PRI>tag: ` prefix and parse what follows.

    A datagram with no JSON in it fails as an assertion naming the bytes
    that arrived. Bare `str.index` raised `ValueError: substring not
    found`, which names neither the channel nor the payload — in a helper
    whose whole job is a legible failure (CR 7).
    """
    start = datagram.find("{")
    if start == -1:
        raise AssertionError(f"audit datagram carries no JSON payload: {datagram!r}")
    return json.loads(datagram[start:])


class SuiteAuditSink:
    """Where the suite's audit records go instead of journald (notifier#84).

    `src.api.main` opens the audit channel at import, for the life of the
    process, and nothing in a test chooses when that import happens — test
    modules do it at collection. Left alone it binds ``/dev/log``, and every
    in-process mint after it lands in ``journalctl -t status-keys`` looking
    exactly like a production one. Pointing ``STATUS_AUDIT_SOCKET`` here
    before collection covers that import, and every subprocess that inherits
    the suite's environment.

    Drained on a thread, not merely bound: an unread ``AF_UNIX`` datagram
    socket fills, and ``SysLogHandler`` then blocks in ``send`` — a hung suite
    in place of a polluted journal.
    """

    def __init__(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="status-audit-")
        self.path = os.path.join(self._dir, "sink.sock")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(self.path)
        self._datagrams: list[str] = []
        self._arrived = threading.Condition()
        self._closing = False
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            try:
                data = self._sock.recv(65536)
            except OSError:
                return
            if self._closing:
                return
            with self._arrived:
                self._datagrams.append(data.decode(errors="replace").rstrip("\x00"))
                self._arrived.notify_all()

    def wait_for(self, needle: str, timeout: float = 5.0) -> dict:
        """Return the first record whose datagram contains *needle*."""

        def match() -> str | None:
            return next((d for d in self._datagrams if needle in d), None)

        with self._arrived:
            found = self._arrived.wait_for(match, timeout)
        if found is None:
            raise AssertionError(f"no audit record containing {needle!r} reached the sink")
        return _audit_payload(found)

    def close(self) -> None:
        """Stop draining and remove the socket file."""
        self._closing = True
        # shutdown() wakes a recv() blocked on another thread; close() alone
        # does not on Linux.
        self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()
        self._thread.join(timeout=5)
        shutil.rmtree(self._dir, ignore_errors=True)


_SUITE_AUDIT_SINK = pytest.StashKey[SuiteAuditSink]()


def pytest_configure(config: pytest.Config) -> None:
    """Point the audit channel at the suite sink before any module is collected."""
    sink = SuiteAuditSink()
    config.stash[_SUITE_AUDIT_SINK] = sink
    os.environ[AUDIT_SOCKET_ENV] = sink.path


def pytest_unconfigure(config: pytest.Config) -> None:
    """Close the suite sink once the session is over."""
    sink = config.stash.get(_SUITE_AUDIT_SINK, None)
    if sink is not None:
        sink.close()


@pytest.fixture(scope="session")
def suite_audit_sink(pytestconfig) -> SuiteAuditSink:
    """The sink every audit record not bound for an `audit_socket` reaches."""
    return pytestconfig.stash[_SUITE_AUDIT_SINK]


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession]:
    """Per-test session wrapped in a savepoint that rolls back on teardown."""
    async with test_engine.connect() as conn:
        txn = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        nested = await conn.begin_nested()

        @event.listens_for(session.sync_session, "after_transaction_end")
        def restart_savepoint(db_session, transaction):
            nonlocal nested
            if not nested.is_active:
                nested = conn.sync_connection.begin_nested()

        yield session

        await session.close()
        await txn.rollback()


@pytest.fixture
async def tenant(db_session) -> Tenant:
    """Create a fresh tenant for the test."""
    t = Tenant(name=f"test-{secrets.token_hex(4)}")
    db_session.add(t)
    await db_session.flush()
    return t


@pytest.fixture
async def api_key(db_session, tenant) -> tuple[str, ApiKey]:
    """Create a test ApiKey; returns (raw_key, ApiKey)."""
    raw = "csk_" + secrets.token_urlsafe(16)
    key = ApiKey(
        tenant_id=tenant.id,
        label="test",
        key_prefix=raw[:8],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
    )
    db_session.add(key)
    await db_session.flush()
    return raw, key


@pytest.fixture
async def client(test_engine, db_session) -> AsyncGenerator[AsyncClient]:
    """An AsyncClient wired to the FastAPI app with the savepointed db_session."""
    from src.api.main import app

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@dataclass
class AuditSocket:
    """A stand-in for journald's ``/dev/log``, and what was sent to it.

    The audit channel's real destination is a datagram socket the kernel
    hands to journald, so a test that asserts on a mock handler proves
    nothing about whether a record would survive the process. Binding a real
    ``AF_UNIX``/``SOCK_DGRAM`` socket and reading the bytes back makes the
    round trip the assertion — and makes it identical on this VM, where
    ``/dev/log`` exists, and in a CI runner where it may not.
    """

    path: str
    sock: socket.socket

    def datagrams(self, expected: int = 1, timeout: float = 5.0) -> list[str]:
        """Read exactly *expected* datagrams, or fail saying what did arrive."""
        deadline = time.monotonic() + timeout
        received: list[str] = []
        while len(received) < expected:
            self.sock.settimeout(max(0.0, deadline - time.monotonic()))
            try:
                received.append(self.sock.recv(65536).decode().rstrip("\x00"))
            except TimeoutError:
                raise AssertionError(
                    f"expected {expected} audit datagram(s), got {len(received)}: {received}"
                ) from None
        return received

    def records(self, expected: int = 1, timeout: float = 5.0) -> list[dict]:
        """Read *expected* datagrams and return their JSON payloads."""
        return [_audit_payload(d) for d in self.datagrams(expected, timeout)]

    def assert_silent(self, timeout: float = 1.0) -> None:
        """Fail if anything at all arrives within *timeout*.

        The positive spelling of "nothing was recorded". Asserting it as
        `pytest.raises(AssertionError, match=...)` around `records()` made a
        helper's failure message load-bearing in another file, and read as
        though the absence of a record were an error (CR 6).
        """
        self.sock.settimeout(timeout)
        try:
            arrived = self.sock.recv(65536).decode().rstrip("\x00")
        except TimeoutError:
            return
        raise AssertionError(f"expected no audit record, got: {arrived!r}")


@pytest.fixture
def audit_socket(tmp_path) -> Iterator[AuditSocket]:
    """Bind a socket at a path `STATUS_AUDIT_SOCKET` can point at."""
    path = str(tmp_path / "audit.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    yield AuditSocket(path=path, sock=sock)
    sock.close()


@pytest.fixture
def run_script(audit_socket, test_engine):
    """Run a credential script the way an operator does: as a real process.

    The two audit records only reach the journal because
    ``configure_script_logging()`` runs under ``if __name__ == "__main__"``,
    which is exactly the line no in-process test of ``main()`` executes. notifier#67
    was a wiring defect at the entry point, so the test has to start at the
    entry point.
    """

    def run(script: str, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, f"scripts/{script}", *argv],
            cwd=Path(__file__).resolve().parents[1],
            env={
                **os.environ,
                "DATABASE_URL": TEST_DATABASE_URL,
                AUDIT_SOCKET_ENV: audit_socket.path,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run
