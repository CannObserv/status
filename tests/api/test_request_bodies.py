"""Every field of every request body takes its own JSON type only (#35).

Pydantic's lax mode coerces across JSON types: ``0`` and ``"off"`` to
``false``, ``"600"`` to 600. Each field's type is read from the OpenAPI
document, so a field or a request body added later is probed too; a value of
any other JSON type must be a 422 naming that field. Within a type, a value
may still be refused or normalised (a lowercase ULID is uppercased): that is
the field's own rule, not a coercion.
"""

import pytest
from ulid import ULID

from src.api.deps import api_key_header
from src.api.main import app

#: A valid body for each request schema; each probe changes one field of it.
#: A new request body fails collection here until it has one.
VALID = {
    "MonitorCreate": {
        "name": "m",
        "interval_seconds": 600,
        "title_template": "T",
        "body_template": "B",
    },
    "MonitorUpdate": {},
    "CheckinRequest": {},
}

#: A value or two of each JSON type, chosen for what lax mode coerces.
PROBES = (
    True,
    False,
    0,
    1,
    600,
    0.0,
    1.0,
    600.0,
    1.5,
    "",
    "0",
    "1",
    "600",
    "600.0",
    "true",
    "yes",
    "off",
    [],
    ["x"],
    {},
    {"a": 1},
    None,
)


def _types(value: object) -> set[str]:
    """The JSON Schema types *value* is an instance of.

    ``600.0`` is an ``integer`` too: JSON Schema's integer is any number
    with no fractional part.
    """
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"integer", "number"} if value.is_integer() else {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    if isinstance(value, dict):
        return {"object"}
    assert value is None
    return {"null"}


def _allowed(schema: dict) -> set[str]:
    """The JSON types a property's schema admits. Fails on a shape it cannot read."""
    if "type" in schema:
        return {schema["type"]}
    if "anyOf" in schema:
        return set().union(*map(_allowed, schema["anyOf"]))
    raise AssertionError(f"no JSON type to probe in {schema}")


def _cases():
    document = app.openapi()
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if "requestBody" not in operation:
                continue
            ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            name = ref.rsplit("/", 1)[-1]
            valid = VALID[name]
            properties = document["components"]["schemas"][name]["properties"]
            for field, schema in properties.items():
                allowed = _allowed(schema)
                for value in PROBES:
                    if not _types(value) & allowed:
                        yield pytest.param(
                            method,
                            path,
                            {**valid, field: value},
                            field,
                            id=f"{name}.{field}={value!r}",
                        )


CASES = list(_cases())


def test_every_request_body_is_probed():
    """Read from the document, so this would pass vacuously were it empty."""
    probed = {case.id.split(".")[0] for case in CASES}
    assert probed == set(VALID)


@pytest.fixture
def headers(api_key) -> dict[str, str]:
    raw_key, _ = api_key
    return {api_key_header.model.name: raw_key}


@pytest.mark.parametrize(("method", "path", "body", "field"), CASES)
async def test_a_value_of_another_json_type_is_422(api, headers, method, path, body, field):
    """Validation answers before the route reads the monitor, so any id serves."""
    url = path.replace("{monitor_id}", str(ULID()))
    response = await api.request(method, url, headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert {tuple(e["loc"][:2]) for e in response.json()["detail"]} == {("body", field)}
