"""Tests for scripts/delete_tenant.py.

There was no sanctioned tenant-deletion path at all until notifier#79: `seed_tenant.py`
creates and `rotate_key.py` attaches, revokes and rotates, so a tenant delete
was ad-hoc SQL against production — the precise failure notifier#62 was filed out of,
still live for the operation that destroys the most.

So, as in `test_rotate_key.py`, most of this is about what the script refuses
to do, plus the two things the ad-hoc `DELETE FROM tenants` could not do at
all: name the victim before it goes, and leave a record behind.
"""

import secrets

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scripts.delete_tenant as delete_tenant_module
from scripts.delete_tenant import (
    ABORTED,
    OK,
    REFUSED,
    main,
    parse_args,
    render,
)
from src.core.api_keys import KeyRecord, mint
from src.core.models import ApiKey, Tenant
from src.core.tenants import TenantInventory

TENANT = "01J0TENANT0000000000000000"


@pytest.fixture
async def live_session(test_engine):
    """A session that really commits.

    ``delete_tenant`` owns its transaction — the audit record follows the
    commit, which is the whole point — so the savepointed ``db_session``
    fixture would hide the behaviour under test.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def live_tenant(live_session):
    """A committed tenant holding two keys; yields its id and name.

    Cleans up on teardown in case the run under test refused, aborted, or
    rehearsed — the cases where the row is still there afterwards, which is
    most of this file.
    """
    name = f"delete-{secrets.token_hex(4)}"
    tenant = Tenant(name=name)
    live_session.add(tenant)
    await live_session.flush()
    await mint(live_session, tenant.id, "first", "production")
    await mint(live_session, tenant.id, "second", "development")
    await live_session.commit()
    tenant_id = str(tenant.id)
    yield tenant_id, name
    await live_session.rollback()
    await live_session.execute(delete(Tenant).where(Tenant.id == tenant_id))
    await live_session.commit()


@pytest.fixture
def factory(monkeypatch, test_engine):
    """Point ``main`` at the test database."""
    monkeypatch.setattr(
        delete_tenant_module,
        "get_session_factory",
        lambda: async_sessionmaker(test_engine, expire_on_commit=False),
    )


async def _exists(session, tenant_id: str) -> bool:
    result = await session.execute(
        select(func.count()).select_from(Tenant).where(Tenant.id == tenant_id)
    )
    return int(result.scalar_one()) == 1


def _inventory(**overrides) -> TenantInventory:
    fields = {
        "tenant_id": TENANT,
        "tenant_name": "acme",
        "keys": [],
    }
    return TenantInventory(**{**fields, **overrides})


class TestArgumentParsing:
    def test_needs_a_tenant_id(self):
        with pytest.raises(SystemExit):
            parse_args([])

    def test_a_tenant_id_is_enough_for_a_rehearsal(self):
        args = parse_args(["--tenant-id", TENANT, "--dry-run"])

        assert args.tenant_id == TENANT
        assert args.dry_run is True

    def test_yes_requires_the_expected_name(self):
        """``--yes`` removes the prompt, and the prompt is the only place a
        human ever reads the name of the tenant they are about to destroy.
        Without one of the two, a mistyped ULID deletes a different consumer
        silently — and unlike a revoke, there is no second key left to notice
        with."""
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT, "--yes"])

    def test_yes_with_an_expected_name_is_accepted(self):
        args = parse_args(["--tenant-id", TENANT, "--yes", "--expect-name", "acme"])

        assert (args.yes, args.expect_name) == (True, "acme")

    def test_a_rehearsal_needs_no_name_and_no_yes(self):
        """A dry run writes nothing, so there is nothing to guard."""
        args = parse_args(["--tenant-id", TENANT, "--dry-run"])

        assert args.expect_name is None

    def test_a_rehearsal_is_the_real_command_plus_one_flag(self):
        """The whole value of `--dry-run` is rehearsing what you are about to
        run. Refusing it alongside `--yes` meant an unattended run had no
        spelling of its own rehearsal, and diverged from `rotate_key.py`,
        which accepts the pair (CR 2)."""
        args = parse_args(["--tenant-id", TENANT, "--expect-name", "acme", "--yes", "--dry-run"])

        assert (args.dry_run, args.yes, args.expect_name) == (True, True, "acme")


def _key(key_id: str, label: str, environment: str) -> KeyRecord:
    return KeyRecord(
        id=key_id,
        tenant_id=TENANT,
        label=label,
        key_prefix="csk_abcde",
        environment=environment,
        created_at=None,
        last_used_at=None,
    )


class TestRender:
    def test_names_the_tenant_and_every_key(self):
        lines = render(
            _inventory(
                keys=[
                    _key("01J0KEY1000000000000000000", "first", "production"),
                    _key("01J0KEY2000000000000000000", "second", "development"),
                ]
            ),
            dry_run=False,
        )
        text = "\n".join(lines)

        assert "tenant_id=01J0TENANT0000000000000000" in text
        assert "tenant_name=acme" in text
        assert "01J0KEY1000000000000000000" in text
        assert "second" in text

    def test_a_rehearsal_says_so_first(self):
        lines = render(_inventory(), dry_run=True)

        assert lines[0] == "DRY RUN — nothing was written"

    def test_never_prints_a_key_hash(self):
        """A key's prefix identifies it to a human; the digest is what
        verifies it, and this is the moment output is most likely to be
        pasted into an incident channel."""
        lines = render(_inventory(keys=[_key("01J0KEY1000000000000000000", "first", "production")]))

        assert not [line for line in lines if "hash" in line]


class TestMain:
    async def test_a_rehearsal_leaves_the_tenant_alone(
        self, factory, live_session, live_tenant, capsys
    ):
        tenant_id, _ = live_tenant

        code = await main(parse_args(["--tenant-id", tenant_id, "--dry-run"]))

        assert code == OK
        assert "DRY RUN" in capsys.readouterr().out
        assert await _exists(live_session, tenant_id)

    async def test_a_rehearsal_with_yes_still_writes_nothing(
        self, factory, live_session, live_tenant, capsys
    ):
        """The unattended run's own rehearsal: same argv, one flag added
        (CR 2). `--yes` skips a prompt this path never reaches."""
        tenant_id, name = live_tenant

        code = await main(
            parse_args(["--tenant-id", tenant_id, "--expect-name", name, "--yes", "--dry-run"])
        )

        assert code == OK
        assert "DRY RUN" in capsys.readouterr().out
        assert await _exists(live_session, tenant_id)

    async def test_deletes_the_tenant(self, factory, live_session, live_tenant):
        tenant_id, name = live_tenant

        code = await main(parse_args(["--tenant-id", tenant_id, "--expect-name", name, "--yes"]))

        assert code == OK
        assert not await _exists(live_session, tenant_id)

    async def test_takes_the_keys_with_it(self, factory, live_session, live_tenant):
        tenant_id, name = live_tenant

        await main(parse_args(["--tenant-id", tenant_id, "--expect-name", name, "--yes"]))

        result = await live_session.execute(
            select(func.count()).select_from(ApiKey).where(ApiKey.tenant_id == tenant_id)
        )
        assert int(result.scalar_one()) == 0

    async def test_refuses_a_tenant_that_does_not_exist(self, factory, capsys):
        code = await main(
            parse_args(["--tenant-id", "01J0NOSUCHTENANT0000000000", "--expect-name", "x", "--yes"])
        )

        assert code == REFUSED
        assert "refused" in capsys.readouterr().err

    async def test_refuses_a_name_that_does_not_match(
        self, factory, live_session, live_tenant, capsys
    ):
        """The guard that catches a pasted ULID belonging to the wrong
        consumer. Nothing is written, and the tenant is still there."""
        tenant_id, _ = live_tenant

        code = await main(
            parse_args(["--tenant-id", tenant_id, "--expect-name", "some-other-tenant", "--yes"])
        )

        assert code == REFUSED
        assert "some-other-tenant" in capsys.readouterr().err
        assert await _exists(live_session, tenant_id)

    async def test_a_mismatched_name_is_checked_under_a_rehearsal_too(
        self, factory, live_tenant, capsys
    ):
        """A dry run that passes where the real run would refuse is a
        rehearsal that certifies the wrong thing."""
        tenant_id, _ = live_tenant

        code = await main(
            parse_args(
                [
                    "--tenant-id",
                    tenant_id,
                    "--expect-name",
                    "some-other-tenant",
                    "--dry-run",
                ]
            )
        )

        assert code == REFUSED

    async def test_refuses_to_act_unattended_without_yes(
        self, factory, live_session, live_tenant, monkeypatch, capsys
    ):
        """A destructive tool that acts on an unattended stdin is one cron
        entry away from deleting a consumer nobody asked it to.

        `sys.stdin.isatty` is pinned rather than left to pytest's capture, as
        `test_rotate_key.py` pins it: under `pytest -s` on a terminal the
        ambient version is true, and the script then blocks on `input()` with
        the whole suite behind it (CR 3)."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        tenant_id, name = live_tenant

        code = await main(parse_args(["--tenant-id", tenant_id, "--expect-name", name]))

        assert code == ABORTED
        assert await _exists(live_session, tenant_id)
        err = capsys.readouterr().err
        assert "About to permanently delete" in err
        assert name in err

    async def test_the_prompt_reaches_a_piped_stdout_run(
        self, factory, live_tenant, monkeypatch, capsys
    ):
        """`input(prompt)` writes to stdout, so an operator piping it — a
        `| tee` of a destructive run — saw the warning, then silence, while
        the script waited on a prompt that had gone into the pipe (CR 10)."""
        tenant_id, name = live_tenant
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: name)

        await main(parse_args(["--tenant-id", tenant_id]))

        captured = capsys.readouterr()
        assert "to proceed" in captured.err
        assert "to proceed" not in captured.out

    async def test_the_typed_name_is_what_proceeds(
        self, factory, live_session, live_tenant, monkeypatch
    ):
        """The prompt's own guard, which nothing else covers: `scripts/` is
        outside coverage's `source = ["src"]`, so an untested branch here is
        invisible rather than merely uncovered (CR 4)."""
        tenant_id, name = live_tenant
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: f"  {name}  ")

        code = await main(parse_args(["--tenant-id", tenant_id]))

        assert code == OK
        assert not await _exists(live_session, tenant_id)

    @pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
    async def test_backing_out_of_the_prompt_aborts_cleanly(
        self, factory, live_session, live_tenant, monkeypatch, capsys, interrupt
    ):
        """Ctrl-D and Ctrl-C are how an operator backs out of a prompt. Both
        raised out of `input()` as a stack trace printed directly beneath
        "This cannot be undone" — and a traceback reads like the script broke
        mid-delete rather than like it declined, which is the ambiguity
        `rotate_key.py` already refuses to create elsewhere (CR 14)."""
        tenant_id, _ = live_tenant
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def raise_it(*_):
            raise interrupt

        monkeypatch.setattr("builtins.input", raise_it)

        code = await main(parse_args(["--tenant-id", tenant_id]))

        assert code == ABORTED
        assert await _exists(live_session, tenant_id)
        assert "Aborted; nothing was written." in capsys.readouterr().err

    async def test_a_wrong_name_at_the_prompt_aborts(
        self, factory, live_session, live_tenant, monkeypatch
    ):
        """Including the empty line a bare Return sends, which is the most
        likely thing to arrive at a prompt nobody meant to answer."""
        tenant_id, _ = live_tenant
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "")

        code = await main(parse_args(["--tenant-id", tenant_id]))

        assert code == ABORTED
        assert await _exists(live_session, tenant_id)
        assert await _exists(live_session, tenant_id)

    async def test_a_refused_database_is_reported_not_raised(self, monkeypatch, capsys):
        """`STATUS_ALLOW_PROD_DB` is in no env file by design, so forgetting
        it is the expected path rather than an edge case."""

        def refuse():
            raise RuntimeError("refusing to connect to production database")

        monkeypatch.setattr(delete_tenant_module, "get_session_factory", refuse)

        code = await main(parse_args(["--tenant-id", TENANT, "--expect-name", "x", "--yes"]))

        assert code == REFUSED
        assert "refused" in capsys.readouterr().err

    async def test_prints_what_it_destroyed(self, factory, live_tenant, capsys):
        tenant_id, name = live_tenant

        await main(parse_args(["--tenant-id", tenant_id, "--expect-name", name, "--yes"]))

        out = capsys.readouterr().out
        assert f"tenant_id={tenant_id}" in out
        assert "first" in out and "second" in out

    async def test_says_where_the_record_landed(self, factory, live_tenant, capsys):
        """A real run's output is otherwise the rehearsal's minus one header
        line. Proving the record exists is the point of the script, so it
        names the channel rather than leaving it to memory (CR 9)."""
        tenant_id, name = live_tenant

        await main(parse_args(["--tenant-id", tenant_id, "--expect-name", name, "--yes"]))

        assert "journalctl -t status-keys" in capsys.readouterr().out

    async def test_a_rehearsal_claims_no_record(self, factory, live_tenant, capsys):
        """It wrote none, so it must not point at a channel that has nothing
        on it."""
        tenant_id, _ = live_tenant

        await main(parse_args(["--tenant-id", tenant_id, "--dry-run"]))

        assert "journalctl" not in capsys.readouterr().out


class TestAuditChannel:
    """The acceptance criterion of notifier#79, read back off a real socket.

    Subprocesses, because ``configure_script_logging()`` runs under
    ``if __name__ == "__main__"`` — the one line an in-process test of
    ``main()`` never executes, and the exact wiring defect notifier#67 was.
    """

    @pytest.fixture
    async def seeded(self, run_script, audit_socket, live_session):
        """A committed tenant holding one key, seeded the way an operator
        would, and cleaned up afterwards."""
        name = f"delete-audit-{secrets.token_hex(4)}"
        done = run_script("seed_tenant.py", name, "only", "production")
        assert done.returncode == 0, done.stderr
        audit_socket.records()  # drain the mint this seeding emitted
        fields = dict(line.split("=", 1) for line in done.stdout.splitlines())
        fields["tenant_name"] = name
        yield fields
        await live_session.rollback()
        await live_session.execute(delete(Tenant).where(Tenant.id == fields["tenant_id"]))
        await live_session.commit()

    def test_every_cascaded_key_leaves_a_record(self, run_script, audit_socket, seeded):
        done = run_script(
            "delete_tenant.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--expect-name",
            seeded["tenant_name"],
            "--yes",
        )
        assert done.returncode == OK, done.stderr

        destroyed, summary = audit_socket.records(expected=2)
        assert destroyed["message"] == "api key destroyed with tenant"
        assert destroyed["key_id"] == seeded["key_id"]
        assert destroyed["tenant_id"] == seeded["tenant_id"]
        assert destroyed["label"] == "only"
        assert destroyed["key_prefix"] == seeded["raw_key"][:8]
        assert summary["message"] == "tenant deleted"
        assert summary["tenant_name"] == seeded["tenant_name"]
        assert summary["keys_destroyed"] == 1

    def test_the_record_never_carries_the_raw_key(self, run_script, audit_socket, seeded):
        """None is held at this point. The assertion belongs with the
        others (notifier#79)."""
        done = run_script(
            "delete_tenant.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--expect-name",
            seeded["tenant_name"],
            "--yes",
        )
        assert done.returncode == OK, done.stderr

        raw = seeded["raw_key"]
        assert not [line for line in audit_socket.datagrams(expected=2) if raw in line]
        assert raw not in done.stdout
        assert raw not in done.stderr

    def test_a_rehearsal_records_nothing(self, run_script, audit_socket, seeded):
        done = run_script("delete_tenant.py", "--tenant-id", seeded["tenant_id"], "--dry-run")
        assert done.returncode == OK, done.stderr

        audit_socket.assert_silent()

    def test_a_refused_run_records_nothing(self, run_script, audit_socket, seeded):
        done = run_script(
            "delete_tenant.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--expect-name",
            "not-this-tenant",
            "--yes",
        )
        assert done.returncode == REFUSED, done.stdout

        audit_socket.assert_silent()
