"""Every field of every request body takes its own JSON type only (#35).

Pydantic's lax mode coerces across JSON types: ``0`` and ``"off"`` to
``false``, ``"600"`` to 600. Each field's type is read from the OpenAPI
document, so a field or a request body added later is probed too; a value of
any other JSON type must be a 422 naming that field. So must the body itself,
each array's items, each typed object's values and each nested model's
fields, at any depth (CR 1). Within a type, a value may still be refused or
normalised (a lowercase ULID is uppercased): that is the field's own rule,
not a coercion.
"""

import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from ulid import ULID

from src.api.deps import api_key_header
from src.api.main import app

#: A valid body for each request schema, and for each model nested in one;
#: each probe changes one value of it. A new one fails collection here until
#: it has an entry.
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

#: Schema keywords that could hold a type this walk does not reach: met, it
#: fails rather than pass what it never probed.
UNREAD = {"allOf", "oneOf", "not", "prefixItems", "patternProperties", "if", "const"}

type Loc = tuple[str | int, ...]


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


class _Walk:
    """Each value with one thing of a wrong JSON type in it, and where that is."""

    def __init__(self, document: dict, valid: dict[str, dict]):
        self.schemas = document["components"]["schemas"]
        self.valid = valid

    def _resolve(self, schema: dict) -> tuple[dict, str | None]:
        """*schema*, its ``$ref`` followed, and the model it names, if any."""
        if "$ref" not in schema:
            return schema, None
        name = schema["$ref"].rsplit("/", 1)[-1]
        return self.schemas[name], name

    def _allowed(self, schema: dict) -> set[str]:
        schema, _ = self._resolve(schema)
        assert not UNREAD & schema.keys(), f"cannot probe {schema}"
        if "type" in schema:
            return {schema["type"]}
        if "anyOf" in schema:
            return set().union(*map(self._allowed, schema["anyOf"]))
        raise AssertionError(f"no JSON type to probe in {schema}")

    def wrong(self, schema: dict) -> Iterator[tuple[object, Loc]]:
        """At *schema*: each value of another type, then each inside one."""
        allowed = self._allowed(schema)
        for value in PROBES:
            if not _types(value) & allowed:
                yield value, ()
        yield from self._inside(schema)

    def _inside(self, schema: dict) -> Iterator[tuple[object, Loc]]:
        schema, name = self._resolve(schema)
        for member in schema.get("anyOf", ()):
            yield from self._inside(member)
        if "items" in schema:
            for value, loc in self.wrong(schema["items"]):
                yield [value], (0, *loc)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for value, loc in self.wrong(extra):
                yield {"k": value}, ("k", *loc)
        if "properties" in schema:
            assert name is not None, f"an inline object: no valid body for {schema}"
            base = self.valid[name]
            for field, inner in schema["properties"].items():
                for value, loc in self.wrong(inner):
                    yield {**base, field: value}, (field, *loc)


def _id(name: str, body: object, loc: Loc) -> str:
    """``MonitorCreate.channel_ids[0]=600``: where in *body*, and what is there."""
    path = ""
    for step in loc:
        path += f"[{step}]" if isinstance(step, int) else f".{step}"
        body = body[step]
    return f"{name}{path}={body!r}"


def _cases(document: dict, valid: dict[str, dict]):
    walk = _Walk(document, valid)
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if "requestBody" not in operation:
                continue
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            name = schema["$ref"].rsplit("/", 1)[-1]
            for body, loc in walk.wrong(schema):
                yield pytest.param(method, path, body, ("body", *loc), id=_id(name, body, loc))


async def _send(client: AsyncClient, method: str, path: str, body: object, headers=None):
    """*body* as the JSON text itself: httpx's ``json=None`` sends no body at all."""
    url = path.replace("{monitor_id}", str(ULID()))
    return await client.request(
        method,
        url,
        content=json.dumps(body),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _refused_at(response, loc: Loc) -> None:
    assert response.status_code == 422, response.text
    for error in response.json()["detail"]:
        assert tuple(error["loc"][: len(loc)]) == loc, error


CASES = list(_cases(app.openapi(), VALID))


def test_every_request_body_is_probed():
    """Read from the document, so this would pass vacuously were it empty."""
    probed = {case.id.split(".")[0].split("=")[0] for case in CASES}
    assert probed == {"MonitorCreate", "MonitorUpdate", "CheckinRequest"}


def test_array_items_are_probed():
    assert any(case.id.startswith("MonitorCreate.channel_ids[0]=") for case in CASES)


class _Inner(BaseModel):
    n: int


class _Lax(BaseModel):
    """Lax where the top level is strict: what the walk must reach (CR 1)."""

    flag: bool
    counts: list[int]
    limits: dict[str, int]
    inner: _Inner
    maybe: list[int] | None = None


async def test_the_walk_finds_lax_coercion_below_the_top_level():
    """The guard's own red: on a body lax inside, the cases that get through
    are the ones nested in it, and each is found."""
    lax = FastAPI()

    @lax.post("/lax")
    async def take(body: _Lax) -> None:
        return None

    valid = {"_Lax": {"flag": True, "counts": [], "limits": {}, "inner": {"n": 1}}, "_Inner": {}}
    through = set()
    async with AsyncClient(transport=ASGITransport(app=lax), base_url="http://t") as client:
        for case in _cases(lax.openapi(), valid):
            method, path, body, loc = case.values
            if (await _send(client, method, path, body)).status_code != 422:
                through.add(loc)
    assert through == {
        ("body", "flag"),
        ("body", "counts", 0),
        ("body", "limits", "k"),
        ("body", "inner", "n"),
        ("body", "maybe", 0),
    }


@pytest.fixture
def headers(api_key) -> dict[str, str]:
    raw_key, _ = api_key
    return {api_key_header.model.name: raw_key}


@pytest.mark.parametrize(("method", "path", "body", "loc"), CASES)
async def test_a_value_of_another_json_type_is_422(api, headers, method, path, body, loc):
    """Validation answers before the route reads the monitor, so any id serves."""
    _refused_at(await _send(api, method, path, body, headers), loc)
