"""Tests for ``.skills/patching-hosts``: the knob's ``bulk-factor`` (#115).

``apply.sh`` budgets the bulk at ``bulk_factor`` times its dry run, 3 when the
knob has no line. This host's bulks ran 2.9x and 3.1x, so the knob sets 3.5.
The first test holds the file itself, parsed with the reader's grammar
(whole-line ``#`` comments and blank lines dropped, whitespace-split) and the
pattern ``_knob-lib.sh`` checks the value against: CI checks out no submodule,
so it can't run the reader. The second runs ``read-knob.sh`` where the vendored
skill is checked out, so a grammar change in a submodule bump fails a local run.
"""

import json
import re
import subprocess

import pytest

from tests.ci.test_dependencies import REPO_ROOT

KNOB = REPO_ROOT / ".skills" / "patching-hosts"
READ_KNOB = REPO_ROOT / "skills" / "patching-hosts" / "scripts" / "read-knob.sh"

#: ``_knob-lib.sh``'s pattern for the value (gregoryfoster/skills#556): 1 to 99.99,
#: at most two decimals.
BULK_FACTOR = re.compile(r"^[1-9][0-9]?([.][0-9][0-9]?)?$")


def bulk_factor_lines() -> list[tuple[int, list[str]]]:
    """Each ``bulk-factor`` line, by line number, as the reader tokenises it."""
    found = []
    for number, line in enumerate(KNOB.read_bytes().decode().split("\n"), start=1):
        tokens = line.split()
        if tokens and tokens[0] == "bulk-factor":
            found.append((number, tokens[1:]))
    return found


def test_knob_sets_bulk_factor_3_5_once():
    """One line, one well-formed value: a second would override the first unseen."""
    lines = bulk_factor_lines()
    assert len(lines) == 1, lines
    _, values = lines[0]
    assert values == ["3.5"]
    assert BULK_FACTOR.match(values[0])


@pytest.mark.skipif(not READ_KNOB.is_file(), reason="skills-vendor not checked out (CI)")
def test_read_knob_reads_bulk_factor_3_5_without_findings():
    """The vendored reader accepts the line: no finding names it."""
    out = subprocess.run(
        ["bash", str(READ_KNOB), "--config", str(KNOB)],
        capture_output=True,
        text=True,
        check=True,
    )
    knob = json.loads(out.stdout)
    assert knob["bulk_factor"]["value"] == 3.5, knob["bulk_factor"]
    number = knob["bulk_factor"]["line"]
    assert [n for n, _ in bulk_factor_lines()] == [number]
    assert not [f for f in knob["findings"] if f.get("line") == number], knob["findings"]
