"""Unit tests for the 422 renderer (#31)."""

import json
import math

from src.api.validation import json_safe


def test_spells_non_finite_numbers_as_strings():
    value = {"input": {"x": [math.nan, math.inf, -math.inf, 1.5]}, "n": None}
    assert json_safe(value) == {"input": {"x": ["NaN", "Infinity", "-Infinity", 1.5]}, "n": None}


def test_its_output_is_strict_json():
    json.dumps(json_safe([{"a": math.nan}]), allow_nan=False)


def test_leaves_everything_else_alone():
    value = {"a": [1, "NaN", True, None, {"b": 2.5}]}
    assert json_safe(value) == value
