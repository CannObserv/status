"""No live Mayfly channel URL in any tracked file (notifier#89).

A Mayfly channel URL, ``/c/<id>#<key>``, is the channel's whole access-control
model: whoever holds it can read the conversation, post into the agents'
working context, and delete the record, with no owner and no revocation. This
repo is public, so a URL that reaches a tracked file is published the moment
it is pushed. The vendored ``using-mayfly-chat`` skill's rule is that the URL
never reaches a durable store; upstream enforces it on its own tree only
(gregoryfoster/skills#302), so this file is that rule enforced here.

Ported from upstream's ``tests/structural/test_no_channel_urls.py``, keeping
its two properties:

- **Match the key, not the host.** The keyless view URL a joiner's first
  ``curl`` returns is harmless, and a guard firing on ``mayfly.chat/c/`` would
  cry wolf every session. The pattern needs the 22-character id **and** the
  ``#`` fragment with its 43-character key, on any host.
- **The pattern cannot match itself.** It is assembled from a character
  class, which no live URL contains, so this file never reports itself.

One addition: the scanner is exercised against a planted file, not only the
regex against a string, so a scan that silently reads nothing — a broken
decode, a filter that drops every path — fails here instead of passing
vacuously.

Scope: every ``git ls-files`` entry that is a regular file and decodes as
UTF-8. Submodule gitlinks are directories and fall out; vendored trees are
upstream's to guard.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

# base64url alphabet; an id is 16 bytes (22 chars), a key 32 bytes (43 chars).
_B64URL = "[A-Za-z0-9_-]"
CHANNEL_URL = re.compile("/c/" + _B64URL + "{22}" + "#" + _B64URL + "{43}")


def _live_url() -> str:
    """Build a URL the pattern must match, at runtime, so no file holds one."""
    return "https://example.test/c/" + "A" * 22 + "#" + "b" * 43


def _tracked_files() -> list[Path]:
    """Return every tracked path that is a regular file on disk."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True)
    paths = [REPO_ROOT / p for p in out.stdout.decode().split("\0") if p]
    return [p for p in paths if p.is_file()]


def _offenders(paths: list[Path], root: Path) -> list[str]:
    """Return ``path:line`` for every channel URL in the UTF-8 files given."""
    found: list[str] = []
    for path in paths:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for match in CHANNEL_URL.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(root)}:{line}")
    return found


class TestDetector:
    """The pattern and the scanner both fire on a live URL, and only on one."""

    def test_pattern_matches_a_live_url(self) -> None:
        assert CHANNEL_URL.search(_live_url())

    def test_pattern_ignores_the_keyless_view_url_and_placeholders(self) -> None:
        assert not CHANNEL_URL.search("https://example.test/c/" + "A" * 22)
        assert not CHANNEL_URL.search("https://example.test/c/<ID>#<key>")

    def test_scanner_names_the_planted_line(self, tmp_path: Path) -> None:
        planted = tmp_path / "notes.md"
        planted.write_text("clean\n" + _live_url() + "\n")
        binary = tmp_path / "blob.bin"
        binary.write_bytes(b"\xff\xfe" + _live_url().encode())
        assert _offenders([planted, binary], tmp_path) == ["notes.md:2"]


class TestTrackedTree:
    """No tracked file holds a channel URL."""

    def test_the_scan_reads_the_tree(self) -> None:
        # Control: a scan over zero files passes whatever the tree holds.
        assert REPO_ROOT / "AGENTS.md" in _tracked_files()

    def test_no_tracked_file_holds_a_channel_url(self) -> None:
        offenders = _offenders(_tracked_files(), REPO_ROOT)
        assert not offenders, (
            "A live Mayfly channel URL (id plus #key) is committed here, which "
            "publishes read, write and delete access to that channel:\n  "
            + "\n  ".join(offenders)
            + "\nRemove it, then treat the channel as leaked: stop the agents "
            "using it, delete it, and distribute a new URL privately "
            "(skills/using-mayfly-chat/references/security.md)."
        )
