"""Tests for src/core/credentials.py — reading a systemd credential (D13)."""

from src.core.credentials import read_credential


def test_reads_the_named_file_under_the_credentials_directory(tmp_path):
    (tmp_path / "some-key").write_text("secret\n")
    assert read_credential("some-key", {"CREDENTIALS_DIRECTORY": str(tmp_path)}) == "secret"


def test_no_credentials_directory_is_no_credential():
    """A hand-run process outside the unit, not a crash."""
    assert read_credential("some-key", {}) == ""


def test_a_missing_file_is_no_credential(tmp_path):
    assert read_credential("some-key", {"CREDENTIALS_DIRECTORY": str(tmp_path)}) == ""


def test_a_whitespace_only_file_is_no_credential(tmp_path):
    """The unit's ``SetCredential=<name>:\\n`` fallback: a lone newline is absent."""
    (tmp_path / "some-key").write_text("\n")
    assert read_credential("some-key", {"CREDENTIALS_DIRECTORY": str(tmp_path)}) == ""


def test_never_read_from_an_environment_variable():
    assert read_credential("some-key", {"SOME_KEY": "leaked"}) == ""
