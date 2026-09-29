"""Secrets reach co-status as systemd credentials, never environment variables (D13).

An ``EnvironmentFile=`` is inherited by every process that sources it and
lands in ``/proc/<pid>/environ``; ``LoadCredential=`` hands the unit a
private, root-sourced file under ``$CREDENTIALS_DIRECTORY`` instead.
"""

import os
from collections.abc import Mapping
from pathlib import Path


def read_credential(name: str, environ: Mapping[str, str] = os.environ) -> str:
    """The credential *name* under ``$CREDENTIALS_DIRECTORY``, or ``""``.

    Absent, unreadable and whitespace-only all read as ``""``: outside the
    unit there is no directory, and a unit may declare a lone-newline
    ``SetCredential=`` fallback so that a missing file does not fail it.
    """
    directory = environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return ""
    try:
        return (Path(directory) / name).read_text().strip()
    except OSError:
        return ""
