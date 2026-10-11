"""Unit tests for StorableJSONObject and refuse_unstorable (#31, #33)."""

import json
import math
import tracemalloc
from typing import Annotated

import pytest
from pydantic import AfterValidator, BaseModel, TypeAdapter, ValidationError
from sqlalchemy import text

from src.api.schemas import types
from src.api.schemas.types import (
    MAX_DEPTH,
    MAX_JSON_BYTES,
    StorableJSONObject,
    TooLarge,
    first_unstorable,
    refuse_unstorable,
)

#: The most a ``jsonb`` container may hold: Postgres's ``JENTRY_OFFLENMASK``.
#: "total size of jsonb array elements exceeds the maximum of 268435455 bytes".
JSONB_MAX_BYTES = 0x0FFFFFFF


class _Body(BaseModel):
    variables: StorableJSONObject


class _Text(BaseModel):
    name: Annotated[str, AfterValidator(refuse_unstorable)]


def _path(value: object) -> str | None:
    found = first_unstorable(value, "v")
    return None if found is None else found[0]


def _nested(depth: int, leaf: object = 1) -> object:
    """*leaf* inside *depth* arrays."""
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def test_finds_nothing_in_storable_json():
    assert first_unstorable({"a": [1, 1.5, {"b": None, "c": "NaN"}], "d": True}, "v") is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_names_the_path_to_a_non_finite_number(bad):
    path, problem = first_unstorable({"a": [0, {"b": bad}]}, "variables")
    assert path == "variables.a[1].b"
    assert problem.startswith("is not a finite number")


def test_a_key_that_is_not_an_identifier_is_quoted():
    assert first_unstorable({"a b": math.nan}, "variables")[0] == 'variables["a b"]'


@pytest.mark.parametrize(
    ("value", "spelled"),
    [
        ("a\x00b", "\\u0000"),
        ("\ud800", "\\ud800"),
        ("\udfff", "\\udfff"),
        ("\ude00\ud83d", "\\ude00"),  # a pair in the wrong order is two lone halves
    ],
)
def test_names_the_path_to_a_string_postgres_refuses(value, spelled):
    path, problem = first_unstorable({"a": [0, {"b": value}]}, "v")
    assert path == "v.a[1].b"
    assert problem.startswith(f"contains {spelled}")


@pytest.mark.parametrize(("key", "spelled"), [("\x00", "\\u0000"), ("\udc00", "\\udc00")])
def test_names_a_key_postgres_refuses_by_its_escape(key, spelled):
    """The path quotes the key with its escape: the 422 stays readable, and
    renderable, whatever the key held."""
    path, problem = first_unstorable({"x": {key: 1}}, "v")
    assert path == f'v.x["{spelled}"]'
    assert problem.startswith(f"has {spelled} in its key")


@pytest.mark.parametrize(
    "value",
    [
        "😀",  # a surrogate pair, as json.loads joins it: one code point
        "\U0010ffff",
        "￿￾﷐",  # noncharacters are still Unicode scalar values
        "\x01\x1f\x7f\x80 ",
        "",
    ],
)
def test_text_postgres_takes_is_storable(value):
    assert first_unstorable({value: value}, "v") is None


def test_the_first_in_document_order_wins():
    """A bad key later in an object does not jump ahead of a bad value earlier."""
    assert _path({"a": {"b": "\x00"}, "c\x00": 1}) == "v.a.b"
    assert _path({"a\x00": {"b": math.nan}}) == 'v["a\\u0000"]'


@pytest.mark.parametrize("value", [1.5, 0, None, True, "ok"])
def test_a_storable_scalar_root_is_storable(value):
    assert first_unstorable(value, "v") is None


def test_a_bare_string_is_named_by_its_root():
    assert first_unstorable("x\x00", "name") == (
        "name",
        "contains \\u0000: valid JSON, but Postgres cannot store it",
    )


def test_nesting_to_the_limit_is_storable():
    """The value itself is level 1: ``{"x": [[...]]}`` with MAX_DEPTH - 1 arrays."""
    assert first_unstorable({"x": _nested(MAX_DEPTH - 1)}, "v") is None


def test_nesting_past_the_limit_names_the_container_that_crosses_it():
    path, problem = first_unstorable({"x": _nested(MAX_DEPTH)}, "v")
    assert path == "v.x" + "[0]" * (MAX_DEPTH - 1)
    assert problem.startswith(f"is nested more than {MAX_DEPTH} levels deep")


def test_a_deep_scalar_does_not_count_as_a_level():
    assert first_unstorable({"x": _nested(MAX_DEPTH - 1, leaf="deep")}, "v") is None


def test_a_body_nested_past_the_recursion_limit_is_walked():
    """json.loads takes nesting deeper than a recursive walk could follow from
    inside a request: the walk must not be the thing that fails."""
    assert _path({"x": _nested(5000)}) == "v.x" + "[0]" * (MAX_DEPTH - 1)


def _peak_bytes(value: object) -> int:
    tracemalloc.start()
    try:
        first_unstorable(value, "v")
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_the_walk_holds_no_entry_per_scalar():
    """A million numbers pass without a path, or a stack entry, built for any:
    the walk runs on the event loop, and a body at the size limit holds millions."""
    assert _peak_bytes({"a": [0] * 1_000_000}) < 1_000_000


def test_a_long_key_is_not_copied_into_every_path_below_it():
    """Built eagerly, ``{"k"*10_000: [0]*10_000}``, 40 KB of JSON, held 96 MB."""
    assert _peak_bytes({"k" * 10_000: [0] * 10_000}) < 1_000_000


def test_a_value_too_deep_to_measure_is_refused_for_its_depth():
    """``json.dumps``, which measures the size, recurses; the walk does not."""
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({"variables": {"x": _nested(50_000)}})
    assert f"nested more than {MAX_DEPTH} levels deep" in caught.value.errors()[0]["msg"]


def test_the_model_refuses_a_non_finite_number_naming_its_path():
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({"variables": {"x": [math.nan]}})
    (error,) = caught.value.errors()
    assert error["loc"] == ("variables",)
    assert "variables.x[0] is not a finite number" in error["msg"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"x": "\x00"}, "variables.x contains \\u0000: valid JSON, but Postgres cannot store it"),
        (
            {"x": ["\ud800"]},
            "variables.x[0] contains \\ud800, a lone surrogate: valid JSON, but not Unicode "
            "text, so Postgres cannot store it nor UTF-8 encode it",
        ),
        (
            {"\x00": 1},
            'variables["\\u0000"] has \\u0000 in its key: valid JSON, but Postgres cannot store it',
        ),
    ],
)
def test_the_model_refuses_a_string_postgres_refuses(value, message):
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({"variables": value})
    (error,) = caught.value.errors()
    assert error["loc"] == ("variables",)
    assert error["msg"] == f"Value error, {message}"


@pytest.mark.parametrize("key", ["\ud800\x00é", "é"])
def test_the_message_is_ascii(key):
    """It is echoed in the 422 and logged: it must encode whatever was refused.
    A key quotes as JSON unless it is an ASCII identifier: ``é`` is one, not ASCII."""
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({"variables": {key: "\ud800"}})
    assert caught.value.errors()[0]["msg"].isascii()


def test_the_model_keeps_storable_json_verbatim():
    body = {"x": [1, 2.5, {"y": None, "z": "😀￿"}]}
    assert _Body.model_validate({"variables": body}).variables == body


def test_the_schema_is_a_plain_object():
    """The check-in contract (D6) sees no change: it is still a free-form object."""
    assert _Body.model_json_schema()["properties"]["variables"] == {
        "additionalProperties": True,
        "title": "Variables",
        "type": "object",
    }


def test_text_refuses_what_postgres_refuses():
    with pytest.raises(ValidationError) as caught:
        _Text.model_validate({"name": "a\udc00"})
    (error,) = caught.value.errors()
    assert error["loc"] == ("name",)
    assert error["msg"].startswith("Value error, name contains \\udc00, a lone surrogate")


def test_text_is_kept_verbatim_and_its_schema_is_a_plain_string():
    assert _Text.model_validate({"name": "é\U0010ffff"}).name == "é\U0010ffff"
    assert _Text.model_json_schema()["properties"]["name"] == {"title": "Name", "type": "string"}


def test_outside_a_model_the_value_is_called_value():
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(StorableJSONObject).validate_python({"x": math.nan})
    assert "value.x is not a finite number" in caught.value.errors()[0]["msg"]


class TestSize:
    """No free-form value may exceed MAX_JSON_BYTES as compact UTF-8 JSON (#33)."""

    @staticmethod
    def _of_size(size: int) -> dict:
        """``{"x": "…"}`` whose compact JSON is *size* bytes, multi-byte text included."""
        overhead = len('{"x":""}') + len("é".encode())
        return {"x": "é" + "a" * (size - overhead)}

    def test_at_the_limit_is_kept(self):
        value = self._of_size(MAX_JSON_BYTES)
        assert len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) == (
            MAX_JSON_BYTES
        )
        assert _Body.model_validate({"variables": value}).variables is not None

    def test_past_the_limit_is_refused_naming_the_field(self):
        with pytest.raises(ValidationError) as caught:
            _Body.model_validate({"variables": self._of_size(MAX_JSON_BYTES + 1)})
        (error,) = caught.value.errors()
        assert isinstance(error["ctx"]["error"], TooLarge)
        assert error["msg"] == (
            f"Value error, variables is {MAX_JSON_BYTES + 1} bytes as JSON, more than the "
            f"{MAX_JSON_BYTES} co-status stores"
        )

    def test_text_past_the_limit_is_refused(self):
        with pytest.raises(ValidationError):
            _Text.model_validate({"name": "a" * MAX_JSON_BYTES})

    def test_the_limit_is_read_when_validating(self, monkeypatch):
        """The route tests lower it rather than send 32 MiB."""
        monkeypatch.setattr(types, "MAX_JSON_BYTES", 8)
        with pytest.raises(ValidationError):
            _Body.model_validate({"variables": {"x": "abcd"}})

    def test_the_worst_case_at_the_limit_fits_in_jsonb_with_room(self):
        """``[0,0,…]`` is the largest ``jsonb`` per byte of JSON: 6 times."""
        assert MAX_JSON_BYTES * 6 < JSONB_MAX_BYTES

    @pytest.mark.parametrize(
        "element", ["0", "[]", "{}", "[0]", "1.5", '""', "null"], ids=lambda e: f"[{e},...]"
    )
    async def test_postgres_takes_no_more_than_six_bytes_per_byte_of_json(
        self, db_session, element
    ):
        """Measured, not assumed: the worst case, ``[0,0,…]``, is exactly 6."""
        body = "[" + ",".join([element] * 100_000) + "]"
        stored = await db_session.scalar(
            text("select pg_column_size(cast(:v as jsonb))"), {"v": body}
        )
        assert stored <= 6 * len(body) + 8
