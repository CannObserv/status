"""Unit tests for the 422 renderer (#31)."""

import json
import math

from fastapi.exceptions import RequestValidationError

from src.api import validation
from src.api.validation import json_safe


def test_spells_non_finite_numbers_as_strings():
    value = {"input": {"x": [math.nan, math.inf, -math.inf, 1.5]}, "n": None}
    assert json_safe(value) == {"input": {"x": ["NaN", "Infinity", "-Infinity", 1.5]}, "n": None}


def test_its_output_is_strict_json():
    json.dumps(json_safe([{"a": math.nan}]), allow_nan=False)


def test_leaves_everything_else_alone():
    value = {"a": [1, "NaN", True, None, {"b": 2.5}]}
    assert json_safe(value) == value


async def test_an_input_too_deep_to_encode_is_left_out(monkeypatch):
    """The fallback, without depending on where the recursion limits fall."""
    exc = RequestValidationError(
        [{"type": "value_error", "loc": ("body", "variables"), "msg": "m", "input": {"x": 1}}]
    )

    def too_deep(value):
        if any("input" in error for error in value):
            raise RecursionError
        return value

    monkeypatch.setattr(validation, "jsonable_encoder", too_deep)
    response = await validation.request_validation_error(None, exc)
    assert response.status_code == 422
    assert json.loads(response.body) == {
        "detail": [{"type": "value_error", "loc": ["body", "variables"], "msg": "m"}]
    }


def test_spells_a_lone_surrogate_by_its_escape():
    """Starlette's JSONResponse encodes UTF-8, which refuses a lone surrogate (#33)."""
    value = {"input": {"k\ud800": ["a\udfffb", "😀"]}, "loc": ["body", "\udc00"]}
    assert json_safe(value) == {
        "input": {"k\\ud800": ["a\\udfffb", "😀"]},
        "loc": ["body", "\\udc00"],
    }


def test_its_output_encodes_as_utf8():
    json.dumps(json_safe({"\ud800": "\udc00", "x": "\x00"}), ensure_ascii=False).encode("utf-8")
