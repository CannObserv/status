"""Which commit this code is: the release's ``REVISION`` (#9, spec R10).

``scripts/deploy.sh`` writes ``REVISION`` last into every release, and a release
is read-only after that, so the file names the code actually running. That holds
for the sweep too, which starts fresh every pass and never had a stamp. A
checkout has no ``REVISION``: a hand-run server or a test reports ``dev``.
"""

from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
REVISION_FILE = "REVISION"
UNSTAMPED = "dev"


def build_id(root: Path = CODE_ROOT) -> str:
    """The release's commit, or ``"dev"`` when there is none.

    Blank counts as none: ``{"build": ""}`` reads as a broken health endpoint
    rather than an unstamped one.
    """
    try:
        return (root / REVISION_FILE).read_text().strip() or UNSTAMPED
    except FileNotFoundError:
        return UNSTAMPED
