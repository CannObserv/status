import json
import subprocess
import sys


def test_dump_openapi_emits_known_paths():
    result = subprocess.run(
        [sys.executable, "scripts/dump_openapi.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    spec = json.loads(result.stdout)
    assert spec["openapi"].startswith("3.")
    paths = set(spec["paths"].keys())
    assert "/health" in paths
    assert "/ready" in paths
