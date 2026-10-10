"""Shared Pydantic field types for API boundary validation."""

import json
import math
import re
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


#: The deepest a free-form value may nest, itself being level 1 (#33). Past
#: about 255 levels pydantic cannot serialize a value, so a monitor holding
#: one could not be read, nor listed with its tenant's others. Half that:
#: notifier wraps what it is sent, and serves it back, the same way.
MAX_DEPTH = 128

#: The largest a free-form value may be, as compact UTF-8 JSON (#33). A
#: ``jsonb`` value holds at most 2^28 - 1 bytes, and takes up to 6 bytes per
#: byte of JSON (``[0,0,…]``): 32 MiB always fits. Read when validating.
MAX_JSON_BYTES = 32 * 1024 * 1024

# Postgres stores neither NUL nor a lone surrogate in text or jsonb, and a
# lone surrogate is not Unicode text, so UTF-8 cannot encode it either.
_UNSTORABLE_CHAR = re.compile("[\x00\ud800-\udfff]")


def _unstorable_char(text: str) -> str | None:
    """The first character of *text* Postgres cannot store, spelled as its escape."""
    found = _UNSTORABLE_CHAR.search(text)
    return None if found is None else f"\\u{ord(found.group()):04x}"


def _why_not(char: str) -> str:
    if char == "\\u0000":
        return "valid JSON, but Postgres cannot store it"
    return "valid JSON, but not Unicode text, so Postgres cannot store it nor UTF-8 encode it"


def first_unstorable(value: object, root: str) -> tuple[str, str] | None:
    """Where the first thing in *value* that a sink refuses is, and what, or None.

    *root* names *value* itself. Keys that are identifiers join with a dot,
    others are quoted as JSON, indices are bracketed:
    ``variables.findings[0]["a b"]``. The problem reads after the path, and
    both are ASCII whatever *value* held. In document order, a key before its
    value. Iterative, so a body nested as deep as ``json.loads`` allows is
    walked rather than failing with ``RecursionError``.

    Refused: ``NaN`` and the infinities (#31); ``\\u0000`` and lone surrogates,
    in strings and keys; nesting past :data:`MAX_DEPTH` (#33).
    """
    # (node, path, depth), or (key, path, None) for a key still to check.
    stack: list[tuple[object, str, int | None]] = [(value, root, 1)]
    while stack:
        node, path, depth = stack.pop()
        if depth is None:
            char = _unstorable_char(node)
            if char is not None:
                return path, f"has {char} in its key: {_why_not(char)}"
            continue
        if isinstance(node, float) and not math.isfinite(node):
            return path, (
                "is not a finite number: NaN, Infinity and -Infinity are not JSON "
                "(RFC 8259 § 6), and numbers must be within a double's range"
            )
        if isinstance(node, str):
            char = _unstorable_char(node)
            if char is not None:
                kind = "" if char == "\\u0000" else ", a lone surrogate"
                return path, f"contains {char}{kind}: {_why_not(char)}"
            continue
        if not isinstance(node, (dict, list)):
            continue
        if depth > MAX_DEPTH:
            return path, (
                f"is nested more than {MAX_DEPTH} levels deep: valid JSON, but deeper "
                "than co-status can serve back"
            )
        children: list[tuple[object, str, int | None]] = []
        if isinstance(node, dict):
            for k, v in node.items():
                member = f"{path}.{k}" if k.isidentifier() else f"{path}[{json.dumps(k)}]"
                children += [(k, member, None), (v, member, depth + 1)]
        else:
            children = [(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(node)]
        stack.extend(reversed(children))  # document order: the first one wins
    return None


def _refuse_unstorable[T](value: T, info: ValidationInfo) -> T:
    """Reject *value* if a sink would refuse it, naming where (#31, #33).

    The walk first: it stops at :data:`MAX_DEPTH`, and ``json.dumps``, which
    measures the size, recurses.
    """
    root = info.field_name or "value"
    found = first_unstorable(value, root)
    if found is not None:
        raise ValueError(" ".join(found))
    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    if size > MAX_JSON_BYTES:
        raise ValueError(
            f"{root} is {size} bytes as JSON, more than the {MAX_JSON_BYTES} co-status stores"
        )
    return value


StorableJSONObject = Annotated[dict[str, Any], AfterValidator(_refuse_unstorable)]
"""A free-form JSON object that every sink here takes (#31, #33).

Python's ``json.loads`` accepts more than Postgres, httpx and pydantic will
hand on. A check-in carrying any of it was a 500 and never recorded, or was
recorded and every read of its monitor a 500:

- ``NaN``, ``Infinity``, ``-Infinity``, and a number past a double's range
  (``1e400``), which ``json.loads`` reads as infinity. Not JSON (#31).
- ``\\u0000``, in a string or a key: valid JSON; ``jsonb`` cannot store it.
- A lone surrogate (``\\ud800``): valid JSON by RFC 8259's grammar, but not
  Unicode text. ``jsonb`` refuses it, and UTF-8 cannot encode it for httpx.
- Nesting past :data:`MAX_DEPTH`, or more than :data:`MAX_JSON_BYTES`.

Refused here, each is a 422 naming the field and the path inside it, before
anything is written or sent; kept, the value is verbatim. The JSON schema
stays a plain ``object``: the check-in contract (D6) is unchanged.
"""

StorableText = Annotated[str, AfterValidator(_refuse_unstorable)]
"""A string Postgres can store in ``text``: no ``\\u0000``, no lone surrogate,
at most :data:`MAX_JSON_BYTES` (#33). The JSON schema stays a plain string."""
