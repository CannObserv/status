"""Tests for scripts/rotate_key.py.

The rotation this script exists for was performed once already, during notifier#57,
as an ad-hoc program written under time pressure while a credential was
exposed. It was correct. The defect is that it had to be composed at all —
the next person, in the same hurry, writes the raw SQL instead (notifier#62).

So the tests here are mostly about what the script *refuses* to do, and about
the one thing the ad-hoc program could not do at all: put the mint and the
revoke in a single transaction.
"""

import secrets
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scripts.rotate_key as rotate_key_module
from scripts.rotate_key import (
    ABORTED,
    NOT_VERIFIED,
    OK,
    REFUSED,
    VERIFY_PATH,
    Outcome,
    apply,
    main,
    parse_args,
    render,
    render_list,
    verify,
)
from src.core.api_keys import KeyRecord, LastKeyError, hash_key, keys_for, mint
from src.core.db_safety import ProductionDatabaseError
from src.core.models import ApiKey, Tenant

TENANT = "01J0TENANT0000000000000000"
KEY = "01J0KEY0000000000000000000"


@pytest.fixture
async def live_session(test_engine):
    """A session that really commits.

    ``apply()`` owns its transaction — that is the guarantee the whole script
    rests on — so the savepointed ``db_session`` fixture would hide exactly
    the behaviour under test.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def live_tenant(live_session):
    """Yields the tenant *id as a string*, not the ORM instance.

    ``apply()`` rolls back on a dry run and on any refusal, and a rollback
    expires every loaded attribute regardless of ``expire_on_commit``. Reading
    ``tenant.id`` afterwards is then a lazy load outside the greenlet — a
    failure about SQLAlchemy internals standing in for the behaviour under
    test.
    """
    tenant = Tenant(name=f"rotate-{secrets.token_hex(4)}")
    live_session.add(tenant)
    await live_session.commit()
    tenant_id = str(tenant.id)
    yield tenant_id
    await live_session.rollback()
    await live_session.execute(delete(Tenant).where(Tenant.id == tenant_id))
    await live_session.commit()


def _raise(exc: Exception):
    """A `get_session_factory` stand-in that fails the way the guard fails."""

    def factory() -> None:
        raise exc

    return factory


@pytest.fixture
def factory(monkeypatch, test_engine):
    """Point ``main`` at the test database.

    Module-level rather than per-class: two classes had grown identical
    copies, the second by copying the first (CR 15).
    """
    monkeypatch.setattr(
        rotate_key_module,
        "get_session_factory",
        lambda: async_sessionmaker(test_engine, expire_on_commit=False),
    )


async def _count(session, tenant_id) -> int:
    result = await session.execute(
        select(func.count()).select_from(ApiKey).where(ApiKey.tenant_id == tenant_id)
    )
    return int(result.scalar_one())


class TestArgumentParsing:
    def test_attaching_needs_only_a_label(self):
        args = parse_args(["--tenant-id", TENANT, "--new-label", "backup"])

        assert args.new_label == "backup"
        assert args.revoke is None
        assert args.environment == "production"

    def test_revoking_needs_only_a_key_id(self):
        args = parse_args(["--tenant-id", TENANT, "--revoke", KEY])

        assert args.revoke == KEY
        assert args.new_label is None

    def test_rotating_is_both_at_once(self):
        args = parse_args(["--tenant-id", TENANT, "--new-label", "new", "--revoke", KEY])

        assert (args.new_label, args.revoke) == ("new", KEY)

    def test_requires_a_tenant_id(self):
        with pytest.raises(SystemExit):
            parse_args(["--new-label", "backup"])

    def test_requires_something_to_do(self):
        """Neither half is not a no-op worth having — it is a typo that would
        otherwise exit 0 and look like a completed rotation."""
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT])

    def test_refuses_an_environment_with_nothing_to_mint(self):
        """--environment with no --new-label reads like it retags the existing
        key. It does not, and silently ignoring it would teach that it does."""
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT, "--revoke", KEY, "--environment", "development"])

    def test_refuses_an_environment_written_with_an_equals_sign(self):
        """`--environment=development` is the same flag as `--environment
        development`, and the guard read argv for the second spelling only —
        so the first sailed past and was silently ignored (CR 1)."""
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT, "--revoke", KEY, "--environment=development"])

    def test_the_environment_default_survives_the_guard(self):
        """The guard needs to tell "not passed" from "passed the default", so
        the default is applied after it rather than by argparse."""
        args = parse_args(["--tenant-id", TENANT, "--new-label", "x"])

        assert args.environment == "production"

    def test_refuses_an_unknown_environment(self):
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT, "--new-label", "x", "--environment", "staging"])

    def test_refuses_force_with_nothing_being_revoked(self):
        """--force permits revoking a tenant's last key. With no --revoke it
        reaches apply() and does nothing — the same silently-ignored flag CR 1
        and CR 3 were about, in the same function (CR 17)."""
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT, "--new-label", "x", "--force"])

    def test_refuses_verify_old_with_nothing_being_revoked(self):
        with pytest.raises(SystemExit):
            parse_args(
                [
                    "--tenant-id",
                    TENANT,
                    "--new-label",
                    "x",
                    "--verify",
                    "http://status:9000",
                    "--verify-old",
                    "csk_whatever",
                ]
            )

    def test_refuses_verify_old_without_a_base_url(self):
        """There is nowhere to send the request that proves the 401."""
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT, "--revoke", KEY, "--verify-old", "csk_whatever"])


class TestApply:
    async def test_attaches_a_key_to_an_existing_tenant(self, live_session, live_tenant):
        """The gap notifier#62 opens with, and what watcher#297 is waiting on."""
        outcome = await apply(
            live_session, tenant_id=live_tenant, new_label="backup", dry_run=False
        )

        assert outcome.raw_key is not None
        assert outcome.minted.label == "backup"
        assert outcome.revoked is None
        assert await _count(live_session, live_tenant) == 1

    async def test_the_attached_key_is_committed(self, live_session, live_tenant):
        outcome = await apply(
            live_session, tenant_id=live_tenant, new_label="backup", dry_run=False
        )
        await live_session.rollback()

        row = (
            (await live_session.execute(select(ApiKey).where(ApiKey.tenant_id == live_tenant)))
            .scalars()
            .one()
        )
        assert row.key_hash == hash_key(outcome.raw_key)

    async def test_rotates_in_one_transaction(self, live_session, live_tenant):
        old, _ = await mint(live_session, live_tenant, "old", "production")
        await live_session.commit()
        old_id = str(old.id)

        outcome = await apply(
            live_session,
            tenant_id=live_tenant,
            new_label="new",
            revoke_id=old_id,
            dry_run=False,
        )

        assert outcome.minted.label == "new"
        assert outcome.revoked.label == "old"
        rows = (
            (await live_session.execute(select(ApiKey).where(ApiKey.tenant_id == live_tenant)))
            .scalars()
            .all()
        )
        assert [r.label for r in rows] == ["new"]

    async def test_a_rotation_never_passes_through_zero_keys(self, live_session, live_tenant):
        """The last-key guard must not fire on a rotation — the replacement is
        already flushed by the time the revoke counts. If this ever needed
        --force, the guard would be refusing the operation it exists for."""
        old, _ = await mint(live_session, live_tenant, "old", "production")
        await live_session.commit()
        old_id = str(old.id)

        outcome = await apply(
            live_session,
            tenant_id=live_tenant,
            new_label="new",
            revoke_id=old_id,
            force=False,
            dry_run=False,
        )

        assert outcome.remaining_keys == 1

    async def test_refuses_to_revoke_the_last_key(self, live_session, live_tenant):
        only, _ = await mint(live_session, live_tenant, "only", "production")
        await live_session.commit()
        only_id = str(only.id)

        with pytest.raises(LastKeyError):
            await apply(live_session, tenant_id=live_tenant, revoke_id=only_id, dry_run=False)

        assert await _count(live_session, live_tenant) == 1

    async def test_force_permits_revoking_the_last_key(self, live_session, live_tenant):
        only, _ = await mint(live_session, live_tenant, "only", "production")
        await live_session.commit()
        only_id = str(only.id)

        outcome = await apply(
            live_session,
            tenant_id=live_tenant,
            revoke_id=only_id,
            force=True,
            dry_run=False,
        )

        assert outcome.remaining_keys == 0
        assert await _count(live_session, live_tenant) == 0


class TestDryRun:
    async def test_writes_nothing(self, live_session, live_tenant):
        old, _ = await mint(live_session, live_tenant, "old", "production")
        await live_session.commit()
        old_id = str(old.id)

        outcome = await apply(
            live_session,
            tenant_id=live_tenant,
            new_label="new",
            revoke_id=old_id,
            dry_run=True,
        )

        assert outcome.dry_run is True
        rows = (
            (await live_session.execute(select(ApiKey).where(ApiKey.tenant_id == live_tenant)))
            .scalars()
            .all()
        )
        assert [r.label for r in rows] == ["old"]

    async def test_withholds_the_raw_key(self, live_session, live_tenant):
        """A dry run rolls the mint back, so the key it generated will never
        authenticate. Printing it would hand an operator a dead credential to
        paste into a consumer's secrets."""
        outcome = await apply(live_session, tenant_id=live_tenant, new_label="backup", dry_run=True)

        assert outcome.raw_key is None
        assert outcome.minted is not None

    async def test_still_refuses_what_the_real_run_would_refuse(self, live_session, live_tenant):
        """A dry run that passes and a real run that fails is worse than no
        dry run — it is a rehearsal that certifies the wrong thing."""
        only, _ = await mint(live_session, live_tenant, "only", "production")
        await live_session.commit()
        only_id = str(only.id)

        with pytest.raises(LastKeyError):
            await apply(live_session, tenant_id=live_tenant, revoke_id=only_id, dry_run=True)


class TestTimestamps:
    def test_renders_iso_8601_with_a_z_suffix(self):
        """AGENTS.md mandates YYYY-MM-DDTHH:MM:SS.ffffffZ, and
        src.core.utils.format_utc_iso is what the rest of the service uses.
        These printed a raw datetime — a shape nothing else here emits, so a
        reader has to convert it by hand (CR 11)."""
        record = KeyRecord(
            id=KEY,
            tenant_id=TENANT,
            label="x",
            key_prefix="csk_abcde",
            environment="production",
            created_at=datetime(2026, 9, 13, 23, 49, 41, 756669, tzinfo=UTC),
            last_used_at=None,
        )

        rendered = "\n".join(render_list(TENANT, [record]))

        assert "created_at=2026-09-13T23:49:41.756669Z" in rendered
        assert "2026-09-13 23:49:41" not in rendered

    def test_an_unused_key_says_never_rather_than_none(self):
        """`last_used_at=None` is Python leaking into an operator's terminal,
        and "never used" is the fact they actually want at revoke time."""
        record = KeyRecord(
            id=KEY,
            tenant_id=TENANT,
            label="x",
            key_prefix="csk_abcde",
            environment="production",
            created_at=datetime(2026, 9, 13, 23, 49, 41, 756669, tzinfo=UTC),
            last_used_at=None,
        )

        rendered = "\n".join(render_list(TENANT, [record]))

        assert "last_used_at=never" in rendered

    def test_the_revoked_block_formats_them_too(self):
        outcome = Outcome(
            tenant_id=TENANT,
            minted=None,
            raw_key=None,
            revoked=KeyRecord(
                id=KEY,
                tenant_id=TENANT,
                label="x",
                key_prefix="csk_abcde",
                environment="production",
                created_at=datetime(2026, 9, 13, 23, 49, 41, 756669, tzinfo=UTC),
                last_used_at=datetime(2026, 9, 14, 1, 2, 3, tzinfo=UTC),
            ),
            dry_run=False,
            remaining_keys=1,
        )

        rendered = "\n".join(render(outcome))

        assert "created_at=2026-09-13T23:49:41.756669Z" in rendered
        assert "last_used_at=2026-09-14T01:02:03Z" in rendered


class TestRender:
    def test_never_prints_the_raw_key_twice(self):
        """The key is shown once, on its own line, in the shape seed_tenant.py
        already uses so an operator's eye lands in the same place."""
        outcome = Outcome(
            tenant_id=TENANT,
            minted=None,
            raw_key="csk_secret",
            revoked=None,
            dry_run=False,
            remaining_keys=1,
        )

        lines = render(outcome)

        assert sum("csk_secret" in line for line in lines) == 1
        assert "raw_key=csk_secret" in lines

    def test_names_the_revoked_key_in_full(
        self,
    ):
        """ "Deleted the other one" is what silently does the wrong thing to a
        tenant holding three keys."""
        revoked = KeyRecord(
            id=KEY,
            tenant_id=TENANT,
            label="compromised",
            key_prefix="csk_abcde",
            environment="production",
            created_at=None,
            last_used_at=None,
        )
        outcome = Outcome(
            tenant_id=TENANT,
            minted=None,
            raw_key=None,
            revoked=revoked,
            dry_run=False,
            remaining_keys=1,
        )

        rendered = "\n".join(render(outcome))

        assert KEY in rendered
        assert "compromised" in rendered
        assert "csk_abcde" in rendered


class TestVerify:
    def _client(self, handler) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_passes_when_the_new_key_is_accepted(self):
        def handler(request):
            assert request.url.path == VERIFY_PATH
            return httpx.Response(200, json=[])

        checks = verify("http://status:9000", new_raw="csk_new", client=self._client(handler))

        assert [c.ok for c in checks] == [True]

    def test_fails_when_the_new_key_is_refused(self):
        """A rotation whose replacement does not work is the failure mode that
        matters most, and the one an unverified rotation hides until the
        consumer's next dispatch."""
        checks = verify(
            "http://status:9000",
            new_raw="csk_new",
            client=self._client(lambda request: httpx.Response(403)),
        )

        assert [c.ok for c in checks] == [False]
        assert "403" in checks[0].detail

    def test_passes_when_the_old_key_is_refused(self):
        def handler(request):
            if request.headers["X-API-Key"] == "csk_old":
                return httpx.Response(401)
            return httpx.Response(200, json=[])

        checks = verify(
            "http://status:9000",
            new_raw="csk_new",
            old_raw="csk_old",
            client=self._client(handler),
        )

        assert [c.ok for c in checks] == [True, True]

    def test_fails_when_the_old_key_still_works(self):
        """The tell that the wrong row was deleted."""
        checks = verify(
            "http://status:9000",
            new_raw="csk_new",
            old_raw="csk_old",
            client=self._client(lambda request: httpx.Response(200, json=[])),
        )

        assert [c.ok for c in checks] == [True, False]

    def test_a_403_on_the_old_key_is_not_proof_of_revocation(self):
        """403 is what a production deployment returns for a development key —
        a key that is still perfectly valid. Only 401 means the row is gone."""

        def handler(request):
            if request.headers["X-API-Key"] == "csk_old":
                return httpx.Response(403)
            return httpx.Response(200, json=[])

        checks = verify(
            "http://status:9000",
            new_raw="csk_new",
            old_raw="csk_old",
            client=self._client(handler),
        )

        assert checks[1].ok is False

    def test_a_malformed_base_url_is_a_failed_check_not_a_crash(self):
        """`httpx.InvalidURL` does not subclass `httpx.HTTPError`, so it
        escaped the handler and surfaced as a traceback — after the rotation
        had already committed, which is the one moment verify() promises not
        to leave an operator guessing. `http://[::1` is a fumbled IPv6 URL,
        and this host's tailnet address has an IPv6 form (CR 10)."""
        checks = verify(
            "http://[::1",
            new_raw="csk_new",
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        )

        assert checks[0].ok is False
        assert "invalid" in checks[0].detail.lower() or "port" in checks[0].detail.lower()

    def test_an_unreachable_endpoint_is_a_failed_check_not_a_crash(self):
        """The rotation has already committed by the time verification runs.
        A traceback here would leave an operator unsure whether it landed."""

        def handler(request):
            raise httpx.ConnectError("no route to host")

        checks = verify("http://status:9000", new_raw="csk_new", client=self._client(handler))

        assert checks[0].ok is False
        assert "no route to host" in checks[0].detail


class TestListing:
    def test_listing_is_something_to_do_on_its_own(self):
        args = parse_args(["--tenant-id", TENANT, "--list"])

        assert args.list is True
        assert args.new_label is None
        assert args.revoke is None

    @pytest.mark.parametrize(
        "extra",
        [
            ["--new-label", "x"],
            ["--revoke", KEY],
            ["--force"],
            ["--verify", "http://status:9000"],
            ["--dry-run"],
            ["--yes"],
            ["--verify-old", "csk_x"],
        ],
        ids=["mint", "revoke", "force", "verify", "dry-run", "yes", "verify-old"],
    )
    def test_refuses_to_combine_listing_with_anything_that_writes(self, extra, capsys):
        """--list is the read an operator does *before* deciding. Bundling it
        with the write would print the state they were about to act on
        alongside the state they already changed.

        The message has to come from the --list guard: --verify-old was
        refused by a later one, which told someone who asked for a listing
        that nothing was being revoked (CR 19).
        """
        with pytest.raises(SystemExit):
            parse_args(["--tenant-id", TENANT, "--list", *extra])

        assert "--list is the read you do before deciding" in capsys.readouterr().err

    async def test_names_every_key_id(self, live_session, live_tenant):
        await mint(live_session, live_tenant, "first", "production")
        await mint(live_session, live_tenant, "second", "development")
        await live_session.commit()

        records = await keys_for(live_session, live_tenant)
        rendered = "\n".join(render_list(live_tenant, records))

        for record in records:
            assert record.id in rendered
            assert record.label in rendered
            assert record.key_prefix in rendered

    async def test_says_so_when_a_tenant_holds_none(self, live_session, live_tenant):
        """An empty table under a heading reads as "the query failed". A
        tenant with no keys is a consumer that cannot authenticate, and the
        line has to say that outright."""
        rendered = "\n".join(render_list(live_tenant, []))

        assert "no keys" in rendered.lower()

    async def test_never_prints_a_hash(self, live_session, live_tenant):
        """The prefix identifies a key; the hash is the verifier. Printing it
        puts an offline-crackable credential digest in a terminal buffer."""
        _key, raw = await mint(live_session, live_tenant, "first", "production")
        await live_session.commit()

        rendered = "\n".join(render_list(live_tenant, await keys_for(live_session, live_tenant)))

        assert hash_key(raw) not in rendered
        assert raw not in rendered


class TestVerificationNeverReportsSuccessHavingCheckedNothing:
    async def test_a_revoke_only_verify_run_does_not_exit_ok(
        self, factory, capsys, live_session, live_tenant
    ):
        """`--verify` on a revoke with no replacement has nothing it can
        probe: the script holds no raw key. It printed no verdict line and
        exited 0, which is indistinguishable from a verification that
        passed (CR 2)."""
        doomed, _ = await mint(live_session, live_tenant, "doomed", "production")
        await mint(live_session, live_tenant, "survivor", "production")
        await live_session.commit()
        doomed_id = str(doomed.id)

        code = await main(
            parse_args(
                [
                    "--tenant-id",
                    live_tenant,
                    "--revoke",
                    doomed_id,
                    "--yes",
                    "--verify",
                    "http://status:9000",
                ]
            )
        )

        out = capsys.readouterr().out
        assert code == NOT_VERIFIED
        assert "nothing could be checked" in out.lower()
        # One instruction, not two. The older "old key not checked" hint ends
        # by telling the operator the same thing (CR 13).
        assert out.lower().count("--verify-old") == 1

    def test_an_uncontrolled_old_key_check_says_so(self):
        """A 401 only means "revoked" if something proves the endpoint would
        have accepted a good key. On a rotation the new-key probe is that
        control; on a revoke-only run there is none (CR 2b)."""
        checks = verify(
            "http://status:9000",
            old_raw="csk_old",
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
        )

        assert checks[0].ok is True
        assert "uncontrolled" in checks[0].name

    def test_a_rotation_labels_the_old_key_check_plainly(self):
        """With a new key in the same run, the 401 has its control and the
        label carries no caveat."""

        def handler(request):
            if request.headers["X-API-Key"] == "csk_old":
                return httpx.Response(401)
            return httpx.Response(200, json=[])

        checks = verify(
            "http://status:9000",
            new_raw="csk_new",
            old_raw="csk_old",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        assert "uncontrolled" not in checks[1].name


class TestRefusalsReachTheOperatorCleanly:
    """A traceback where a one-line refusal belongs is what sends someone to
    raw SQL. These ran as tracebacks until the script was exercised by hand.
    """

    async def test_the_last_key_refusal_is_a_message_not_a_traceback(
        self, factory, capsys, live_session, live_tenant
    ):
        only, _ = await mint(live_session, live_tenant, "only", "production")
        await live_session.commit()
        only_id = str(only.id)

        code = await main(parse_args(["--tenant-id", live_tenant, "--revoke", only_id, "--yes"]))

        captured = capsys.readouterr()
        assert code == REFUSED
        assert "Traceback" not in captured.err
        assert "last key" in captured.err

    async def test_an_unknown_tenant_is_a_message_not_a_traceback(self, factory, capsys):
        code = await main(parse_args(["--tenant-id", TENANT, "--new-label", "whatever", "--yes"]))

        captured = capsys.readouterr()
        assert code == REFUSED
        assert "Traceback" not in captured.err
        assert TENANT in captured.err

    async def test_listing_an_unknown_tenant_is_a_message_not_a_traceback(self, factory, capsys):
        code = await main(parse_args(["--tenant-id", TENANT, "--list"]))

        captured = capsys.readouterr()
        assert code == REFUSED
        assert "Traceback" not in captured.err

    async def test_a_refusal_leaves_stdout_empty(self, factory, capsys, live_session, live_tenant):
        """Nothing happened, so nothing belongs on the channel an operator
        pipes into a secrets file."""
        only, _ = await mint(live_session, live_tenant, "only", "production")
        await live_session.commit()
        only_id = str(only.id)

        await main(parse_args(["--tenant-id", live_tenant, "--revoke", only_id, "--yes"]))

        assert capsys.readouterr().out == ""


class TestTheProductionGuardRefusesCleanly:
    """Forgetting STATUS_ALLOW_PROD_DB=1 is the designed-for mistake, not an
    edge case: the opt-in is deliberately in no env file, so an operator has
    to remember it every single time. It arrived as six lines of stack (CR 18).
    """

    @pytest.fixture
    def production_url(self, monkeypatch):
        monkeypatch.setattr(
            rotate_key_module,
            "get_session_factory",
            _raise(ProductionDatabaseError("Refusing to open production database 'status'.")),
        )

    async def test_it_is_a_message_not_a_traceback(self, production_url, capsys):
        code = await main(parse_args(["--tenant-id", TENANT, "--list"]))

        captured = capsys.readouterr()
        assert code == REFUSED
        assert "Traceback" not in captured.err
        assert "Refusing to open production database" in captured.err

    async def test_the_write_path_refuses_too(self, production_url, capsys):
        code = await main(parse_args(["--tenant-id", TENANT, "--new-label", "x", "--yes"]))

        captured = capsys.readouterr()
        assert code == REFUSED
        assert "Traceback" not in captured.err
        assert captured.out == ""

    async def test_an_unset_database_url_refuses_too(self, monkeypatch, capsys):
        """`get_database_url` raises a bare RuntimeError for that one."""
        monkeypatch.setattr(
            rotate_key_module,
            "get_session_factory",
            _raise(RuntimeError("DATABASE_URL environment variable is not set.")),
        )

        code = await main(parse_args(["--tenant-id", TENANT, "--list"]))

        assert code == REFUSED
        assert "DATABASE_URL" in capsys.readouterr().err


class TestConfirmation:
    async def test_refuses_to_act_unattended_without_yes(self, monkeypatch, capsys):
        """An incident tool that acts on an unattended stdin is one cron entry
        away from rotating a key nobody asked it to."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        code = await main(parse_args(["--tenant-id", TENANT, "--new-label", "nope"]))

        assert code == ABORTED
        assert capsys.readouterr().out == ""

    async def test_the_prompt_describes_the_operation_on_stderr(self, monkeypatch, capsys):
        """Ordered with the refusal that follows it, and off the channel an
        operator may be piping."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        await main(parse_args(["--tenant-id", TENANT, "--new-label", "nope"]))

        err = capsys.readouterr().err
        assert "About to" in err
        assert "nope" in err

    async def test_the_prompt_itself_reaches_a_piped_stdout_run(
        self, factory, live_tenant, monkeypatch, capsys
    ):
        """`input(prompt)` writes to stdout, where this script's raw key also
        goes — so an operator piping it to keep a copy saw the description on
        stderr, then silence, while the script waited on a prompt that had
        gone into the pipe. The description and the prompt it introduces
        belong on the same stream (CR 10, first fixed in delete_tenant.py).
        """
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "yes")

        code = await main(parse_args(["--tenant-id", live_tenant, "--new-label", "piped"]))

        assert code == OK
        captured = capsys.readouterr()
        assert "Type 'yes' to proceed" in captured.err
        assert "Type 'yes'" not in captured.out

    @pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
    async def test_backing_out_of_the_prompt_aborts_cleanly(
        self, factory, live_tenant, monkeypatch, capsys, interrupt
    ):
        """Ctrl-D and Ctrl-C both raised out of `input()` as a stack trace.
        This module's own refusal handler says why that is wrong: a traceback
        reads like the script broke rather than like it declined, and an
        operator mid-incident resolves that ambiguity by reaching for psql
        (CR 14)."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def raise_it(*_):
            raise interrupt

        monkeypatch.setattr("builtins.input", raise_it)

        code = await main(parse_args(["--tenant-id", live_tenant, "--new-label", "nope"]))

        assert code == ABORTED
        assert "Aborted; nothing was written." in capsys.readouterr().err


class TestAuditChannel:
    """A revoke run by hand leaves a record that outlives the shell (notifier#67).

    The half notifier#62's trade rests on. A mint at least leaves the `api_keys` row
    and its `created_at`; a revoke deletes the row, so if the record is dropped
    there is nothing anywhere that says which key died or when. Three
    production credential changes on 2026-09-14 are exactly that hole, and
    predate this channel.

    Subprocesses, because `configure_script_logging()` runs under
    `if __name__ == "__main__"` — the one line an in-process test of `main()`
    never executes.
    """

    @pytest.fixture
    async def seeded(self, run_script, audit_socket, live_session):
        """A committed tenant holding one key, and its ids.

        Cleans up on teardown like `live_tenant` does. These rows really
        commit — a subprocess owns its own transaction — so without this they
        accumulate until the session's `drop_all`, and the file's own fixture
        already sets the convention.
        """
        done = run_script(
            "seed_tenant.py", f"rotate-audit-{secrets.token_hex(4)}", "old", "production"
        )
        assert done.returncode == 0, done.stderr
        audit_socket.records()  # drain the mint this seeding emitted
        fields = dict(line.split("=", 1) for line in done.stdout.splitlines())
        yield fields
        await live_session.rollback()
        await live_session.execute(delete(Tenant).where(Tenant.id == fields["tenant_id"]))
        await live_session.commit()

    def test_a_rotation_records_both_halves(self, run_script, audit_socket, seeded):
        done = run_script(
            "rotate_key.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--new-label",
            "replacement",
            "--revoke",
            seeded["key_id"],
            "--yes",
        )
        assert done.returncode == OK, done.stderr

        minted, revoked = audit_socket.records(expected=2)
        assert minted["message"] == "api key minted"
        assert minted["label"] == "replacement"
        assert revoked["message"] == "api key revoked"
        assert revoked["key_id"] == seeded["key_id"]
        assert revoked["label"] == "old"
        assert revoked["tenant_id"] == seeded["tenant_id"]

    def test_a_bare_revoke_still_records_what_it_deleted(self, run_script, audit_socket, seeded):
        """The case that leaves nothing behind: no replacement row, no
        `created_at`, nothing but this record."""
        run_script(
            "rotate_key.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--new-label",
            "survivor",
            "--yes",
        )
        audit_socket.records()  # drain that mint

        done = run_script(
            "rotate_key.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--revoke",
            seeded["key_id"],
            "--yes",
        )
        assert done.returncode == OK, done.stderr

        (record,) = audit_socket.records()
        assert record["message"] == "api key revoked"
        assert record["key_id"] == seeded["key_id"]
        assert record["key_prefix"] == seeded["raw_key"][:8]

    def test_the_raw_key_reaches_stdout_and_nowhere_else(self, run_script, audit_socket, seeded):
        """`test_never_prints_the_raw_key_twice` holds for the rendered lines;
        this holds it across every channel the process writes to."""
        done = run_script(
            "rotate_key.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--new-label",
            "replacement",
            "--revoke",
            seeded["key_id"],
            "--yes",
        )
        raw = next(
            line.removeprefix("raw_key=")
            for line in done.stdout.splitlines()
            if line.startswith("raw_key=")
        )

        assert done.stdout.count(raw) == 1
        assert not [line for line in audit_socket.datagrams(expected=2) if raw in line]
        assert raw not in done.stderr

    def test_a_refused_run_records_nothing(self, run_script, audit_socket, seeded):
        """The last-key guard writes nothing to the database, so it must leave
        nothing on a channel an operator reads as a list of what happened."""
        done = run_script(
            "rotate_key.py",
            "--tenant-id",
            seeded["tenant_id"],
            "--revoke",
            seeded["key_id"],
            "--yes",
        )
        assert done.returncode == REFUSED, done.stdout

        audit_socket.assert_silent()
