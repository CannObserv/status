"""Shared Pydantic field types for API boundary validation."""

from typing import Annotated

from pydantic import AfterValidator, WithJsonSchema
from ulid import ULID
from ulid import base32 as _ulid_base32

# Derived from ulid.base32.ENCODE — the library's canonical Crockford alphabet.
# Letters included in both cases: the runtime normalises lowercase → uppercase before
# parsing, so the OpenAPI pattern and the runtime validator accept the same input space.
_ULID_ALPHA = "".join(c for c in _ulid_base32.ENCODE if c.isalpha())
_ULID_PATTERN = f"^[{_ulid_base32.ENCODE}{_ULID_ALPHA.lower()}]{{26}}$"


def _normalise_ulid(value: str) -> str:
    """Uppercase a ULID string and reject anything ULID cannot parse.

    ``_ULID_PATTERN`` documents the accepted input space in OpenAPI but does
    not enforce it: a constraint would run before this validator and answer
    "String should match pattern" where "invalid ULID" names the actual
    problem.
    """
    normalised = value.upper()
    try:
        ULID.from_str(normalised)
    except ValueError:
        raise ValueError(f"invalid ULID: {value!r}")
    return normalised


ULIDStr = Annotated[
    str,
    AfterValidator(_normalise_ulid),
    WithJsonSchema({"type": "string", "pattern": _ULID_PATTERN}),
]
"""26-char Crockford base32 ULID string, normalised to uppercase.

Use on all ID fields that cross the API boundary (path params, request body
``*_id`` fields). Invalid values produce a 422 with the field path rather than
a silent 404.

An ``Annotated`` alias rather than a ``str`` subclass with
``__get_pydantic_core_schema__``: the subclass form had to build its core
schema through ``pydantic_core``, a package this project never declared and
whose compiled internals are versioned separately from pydantic itself (notifier#32).
Every use site is an annotation, so the two forms are interchangeable there.
"""
