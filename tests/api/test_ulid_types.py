"""Unit tests for the ULIDStr Pydantic validator."""

import re

import pytest
from pydantic import BaseModel, ValidationError

from src.api.schemas.types import _ULID_PATTERN, ULIDStr, _normalise_ulid


def test_valid_ulid_passes():
    assert _normalise_ulid("01H5K3G8V4HCQ2DXFE5FJNWQER") == "01H5K3G8V4HCQ2DXFE5FJNWQER"


def test_normalizes_to_uppercase():
    result = _normalise_ulid("01h5k3g8v4hcq2dxfe5fjnwqer")
    assert result == "01H5K3G8V4HCQ2DXFE5FJNWQER"


def test_all_zeros_valid():
    assert _normalise_ulid("00000000000000000000000000") == "00000000000000000000000000"


def test_uuid_with_dashes_invalid():
    with pytest.raises(ValueError, match="invalid ULID"):
        _normalise_ulid("550e8400-e29b-41d4-a716-446655440000")


def test_too_short_invalid():
    with pytest.raises(ValueError, match="invalid ULID"):
        _normalise_ulid("01H5K3G8V4HCQ2")


def test_too_long_invalid():
    with pytest.raises(ValueError, match="invalid ULID"):
        _normalise_ulid("01H5K3G8V4HCQ2DXFE5FJNWQER0")


def test_empty_string_invalid():
    with pytest.raises(ValueError, match="invalid ULID"):
        _normalise_ulid("")


def test_random_string_invalid():
    with pytest.raises(ValueError, match="invalid ULID"):
        _normalise_ulid("not-a-valid-ulid")


def test_pydantic_model_accepts_valid_ulid():
    class M(BaseModel):
        id: ULIDStr

    m = M(id="01h5k3g8v4hcq2dxfe5fjnwqer")
    assert m.id == "01H5K3G8V4HCQ2DXFE5FJNWQER"
    assert isinstance(m.id, str)


def test_pydantic_model_rejects_invalid():
    class M(BaseModel):
        id: ULIDStr

    with pytest.raises(ValidationError) as exc_info:
        M(id="not-a-ulid")
    assert "invalid ULID" in str(exc_info.value)


def test_pydantic_model_rejects_a_non_string():
    """A non-string id answers `string_type`, not `invalid ULID` (notifier#32).

    The str subclass this type replaced used a plain validator that
    stringified anything, so `123` reached ULID parsing and came back as
    "invalid ULID: 123". Same 422 and same `loc` either way; pinned here
    because it is a deliberate consequence of the rewrite rather than an
    accident, and the SDK surfaces `msg` to consumers.
    """

    class M(BaseModel):
        id: ULIDStr

    with pytest.raises(ValidationError) as exc_info:
        M(id=123)
    assert exc_info.value.errors()[0]["type"] == "string_type"


def test_pydantic_model_optional_accepts_none():
    class M(BaseModel):
        id: ULIDStr | None = None

    assert M(id=None).id is None
    assert M().id is None


def test_pydantic_model_list_validates_each_element():
    class M(BaseModel):
        ids: list[ULIDStr]

    m = M(ids=["01H5K3G8V4HCQ2DXFE5FJNWQER", "00000000000000000000000000"])
    assert m.ids[0] == "01H5K3G8V4HCQ2DXFE5FJNWQER"

    with pytest.raises(ValidationError):
        M(ids=["01H5K3G8V4HCQ2DXFE5FJNWQER", "not-a-ulid"])


def test_ulid_pattern_matches_lowercase():
    """_ULID_PATTERN accepts lowercase Crockford chars — matching the runtime normalisation."""
    assert re.match(_ULID_PATTERN, "01h5k3g8v4hcq2dxfe5fjnwqer")
