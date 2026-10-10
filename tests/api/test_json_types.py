"""Unit tests for FiniteJSONObject (#31)."""

import math

import pytest
from pydantic import BaseModel, ValidationError

from src.api.schemas.types import FiniteJSONObject, non_finite_path


class _Body(BaseModel):
    variables: FiniteJSONObject


def test_finds_nothing_in_finite_json():
    assert non_finite_path({"a": [1, 1.5, {"b": None, "c": "NaN"}], "d": True}, "v") is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_names_the_path_to_a_non_finite_number(bad):
    assert non_finite_path({"a": [0, {"b": bad}]}, "variables") == "variables.a[1].b"


def test_a_key_that_is_not_an_identifier_is_quoted():
    assert non_finite_path({"a b": math.nan}, "variables") == 'variables["a b"]'


def test_the_model_refuses_one_naming_its_path():
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({"variables": {"x": [math.nan]}})
    (error,) = caught.value.errors()
    assert error["loc"] == ("variables",)
    assert "variables.x[0]" in error["msg"]


def test_the_model_keeps_finite_json_verbatim():
    body = {"x": [1, 2.5, {"y": None}]}
    assert _Body.model_validate({"variables": body}).variables == body


def test_the_schema_is_a_plain_object():
    """The check-in contract (D6) sees no change: it is still a free-form object."""
    assert _Body.model_json_schema()["properties"]["variables"] == {
        "additionalProperties": True,
        "title": "Variables",
        "type": "object",
    }


def test_a_body_nested_past_the_recursion_limit_is_walked():
    """json.loads takes nesting deeper than a recursive walk could follow from
    inside a request: the walk must not be the thing that fails."""
    value: object = math.nan
    for _ in range(5000):
        value = [value]
    assert non_finite_path({"x": value}, "v") == "v.x" + "[0]" * 5000
