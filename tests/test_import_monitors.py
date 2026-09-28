"""The operator's side of the import: arguments, the tenant guard, the output."""

import json

import pytest

from scripts.import_monitors import parse_args, render
from src.core.importer import ImportedMonitor


class TestArguments:
    def test_needs_a_row_a_tenant_and_the_expected_name(self):
        with pytest.raises(SystemExit):
            parse_args([])

    def test_channels_are_old_equals_new(self, tmp_path):
        row = tmp_path / "row.json"
        row.write_text(json.dumps({"id": "x"}))
        args = parse_args(
            [
                "--row",
                str(row),
                "--expect-tenant",
                "co-broker",
                "--channel",
                "A=B",
                "--channel",
                "C=D",
            ]
        )
        assert args.channel == [("A", "B"), ("C", "D")]
        assert args.dry_run is False

    def test_a_malformed_channel_is_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            parse_args(["--row", "r.json", "--expect-tenant", "t", "--channel", "nope"])


def test_render_names_what_was_imported_and_says_when_it_was_a_rehearsal():
    lines = render(
        ImportedMonitor(
            id="01J00000000000000000M0NTR1",
            tenant_name="co-watcher",
            name="co-watcher-backup",
            channel_ids=["N1"],
            state="ok",
            last_checkin_at="2026-09-25T03:20:22.248198Z",
        ),
        dry_run=True,
    )
    assert lines[0] == "DRY RUN — nothing was written"
    assert "monitor_id=01J00000000000000000M0NTR1" in lines
    assert "enabled=false" in lines


async def test_refuses_the_wrong_tenant_before_writing(run_script, tmp_path, db_session):
    """--expect-tenant is the guard: the row names the tenant, and a typo in a
    hand-edited export must not land a monitor on another consumer."""
    row = tmp_path / "row.json"
    row.write_text(json.dumps({"tenant_name": "co-broker", "id": "x"}))
    done = run_script(
        "import_monitors.py", "--row", str(row), "--expect-tenant", "co-index", "--dry-run"
    )
    assert done.returncode == 2
    assert "co-index" in done.stderr
