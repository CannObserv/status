"""The check-in contract is notifier's, byte for byte on the wire (spec D6).

Consumers switch by base URL and key alone, so co-status's check-in request,
202 response and path parameter must stay exactly what notifier published at
the commit the snapshot names. Descriptions are prose, not wire format, and
are compared out.
"""

import json
from pathlib import Path

import pytest

from src.api.main import app

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "notifier-checkin-contract.json"
    ).read_text()
)


def _wire(value):
    """Drop documentation keys, recursively; keep everything a parser sees."""
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items() if k not in {"description", "summary"}}
    if isinstance(value, list):
        return [_wire(v) for v in value]
    return value


@pytest.fixture(scope="module")
def spec() -> dict:
    return app.openapi()


@pytest.fixture(scope="module")
def operation(spec) -> dict:
    return spec["paths"][FIXTURE["path"]]["post"]


def test_the_snapshot_names_its_source():
    assert FIXTURE["_source"].startswith("CannObserv/notifier@2c02dbf")


def test_the_path_parameter_matches(operation):
    assert _wire(operation["parameters"]) == _wire(FIXTURE["parameters"])


def test_the_request_body_matches(operation):
    assert _wire(operation["requestBody"]) == _wire(FIXTURE["requestBody"])


def test_the_202_matches(operation):
    assert _wire(operation["responses"]["202"]) == _wire(FIXTURE["response_202"])


@pytest.mark.parametrize("name", sorted(FIXTURE["schemas"]))
def test_every_schema_on_the_wire_matches(spec, name):
    assert _wire(spec["components"]["schemas"][name]) == _wire(FIXTURE["schemas"][name])
