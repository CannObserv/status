import json
import subprocess
import sys

from src.api.schemas.types import _ULID_PATTERN


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
    assert "/api/v1/monitors" in paths
    assert "/api/v1/monitors/{monitor_id}/checkin" in paths


def test_dump_openapi_ulid_pattern_in_spec():
    """ULIDStr fields emit the ULID regex pattern in the OpenAPI spec."""
    result = subprocess.run(
        [sys.executable, "scripts/dump_openapi.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert _ULID_PATTERN in result.stdout
