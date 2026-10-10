"""Shared Pydantic field types for API boundary validation."""

import json
import math
from typing import Annotated, Any

from pydantic import AfterValidator, ValidationInfo, WithJsonSchema
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


def non_finite_path(value: object, root: str) -> str | None:
    """The path to the first ``NaN``, ``Infinity`` or ``-Infinity`` in *value*, or None.

    *root* names *value* itself. Keys that are identifiers join with a dot,
    others are quoted, indices are bracketed: ``variables.findings[0]["a b"]``.
    Iterative, so a body nested as deep as ``json.loads`` allows is walked
    rather than failing with ``RecursionError``.
    """
    stack: list[tuple[object, str]] = [(value, root)]
    while stack:
        node, path = stack.pop()
        if isinstance(node, float) and not math.isfinite(node):
            return path
        if isinstance(node, dict):
            children = [
                (v, f"{path}.{k}" if k.isidentifier() else f"{path}[{json.dumps(k)}]")
                for k, v in node.items()
            ]
        elif isinstance(node, list):
            children = [(v, f"{path}[{i}]") for i, v in enumerate(node)]
        else:
            continue
        stack.extend(reversed(children))  # document order: the first one wins
    return None


def _refuse_non_finite(value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
    """Reject a non-finite number anywhere in *value*, naming where (#31)."""
    path = non_finite_path(value, info.field_name or "value")
    if path is not None:
        raise ValueError(
            f"{path} is not a finite number: NaN, Infinity and -Infinity are not JSON "
            "(RFC 8259 § 6), and numbers must be within a double's range"
        )
    return value


FiniteJSONObject = Annotated[dict[str, Any], AfterValidator(_refuse_non_finite)]
"""A free-form JSON object holding no ``NaN``, ``Infinity`` or ``-Infinity``.

Python's ``json.loads`` accepts those literals, and reads a number past a
double's range (``1e400``) as infinity. Postgres ``jsonb`` and httpx's encoder
refuse all of them, so one inside a check-in was a 500 and a check-in never
recorded (#31). Refused here, it is a 422 naming the field and the path inside
it, before anything is written or sent. The JSON schema stays a plain
``object``: the check-in contract (D6) is unchanged.
"""
