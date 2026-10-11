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
#: 255 levels pydantic cannot serialize a value, so a monitor holding one
#: could not be read, nor listed with its tenant's others. Half that: the
#: 202 serves ``metadata`` back three levels down, in
#: ``dispatches[].metadata``, and notifier's own ``DispatchOut`` serializes
#: it with pydantic too.
MAX_DEPTH = 128

#: The largest a free-form value may be, as compact UTF-8 JSON (#33). A
#: ``jsonb`` value holds at most 2^28 - 1 bytes, and takes up to 6 bytes per
#: byte of JSON (``[0,0,…]``): 32 MiB always fits. Read when validating.
MAX_JSON_BYTES = 32 * 1024 * 1024


class TooLarge(ValueError):
    """A value refused for its size. Its 422 leaves out the ``input``, which
    would be as large again; pydantic keeps the exception as ``ctx["error"]``."""


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


#: Where a node sits: its parent's ``_Where`` and its key or index, or None
#: for the root. Built per container, and a path only for what is refused.
type _Where = tuple[_Where | None, str | int] | None


def _path(root: str, where: _Where) -> str:
    """*where* as a path from *root*: ``variables.findings[0]["a b"]``."""
    steps: list[str | int] = []
    while where is not None:
        where, step = where
        steps.append(step)
    path = root
    for step in reversed(steps):
        if isinstance(step, int):
            path += f"[{step}]"
        elif step.isascii() and step.isidentifier():
            path += f".{step}"
        else:
            path += f"[{json.dumps(step)}]"
    return path


#: Scalars no sink refuses: a container holding only these needs no loop.
_CANNOT_FAIL = {int, bool, type(None)}


def _refused(node: object) -> bool:
    """Whether *node*, a scalar or a key, is itself what a sink refuses."""
    if isinstance(node, str):
        return _UNSTORABLE_CHAR.search(node) is not None
    return isinstance(node, float) and not math.isfinite(node)


def first_unstorable(value: object, root: str) -> tuple[str, str] | None:
    """Where the first thing in *value* that a sink refuses is, and what, or None.

    *value* is JSON as ``json.loads`` gives it: its keys are ``str``. *root*
    names *value* itself. Keys that are ASCII identifiers join with a
    dot, others are quoted as JSON, indices are bracketed:
    ``variables.findings[0]["a b"]``. The path is ASCII whatever *value*
    held, and the problem fixed text. In document order, a key before its value.

    Iterative, so a body nested as deep as ``json.loads`` allows is walked
    rather than failing with ``RecursionError``. It runs on the event loop
    over bodies of up to :data:`MAX_JSON_BYTES`: only containers, and what is
    refused, are put on its stack, and a path is built only for what is. Its
    cost is still a Python step per container and string: at 32 MiB, about
    4 s for strings and 12 s for small objects, beside the 1.6 s
    ``json.loads`` and 1.5 s ``json.dumps`` any body that size costs.

    Refused: ``NaN`` and the infinities (#31); ``\\u0000`` and lone surrogates,
    in strings and keys; nesting past :data:`MAX_DEPTH` (#33).
    """
    # (node, where, depth); depth None for a key, which is what is refused.
    stack: list[tuple[object, _Where, int | None]] = [(value, None, 1)]
    while stack:
        node, where, depth = stack.pop()
        if isinstance(node, dict | list):
            if depth > MAX_DEPTH:
                return _path(root, where), (
                    f"is nested more than {MAX_DEPTH} levels deep: valid JSON, but deeper "
                    "than co-status can serve back"
                )
            values = node.values() if isinstance(node, dict) else node
            keys = node if isinstance(node, dict) else ()
            if set(map(type, values)) <= _CANNOT_FAIL and not any(map(_refused, keys)):
                continue  # nothing to push, found in C: a million numbers are not a loop
            # Reversed onto the stack: document order, so the first one wins.
            if isinstance(node, dict):
                for k, v in reversed(node.items()):
                    if isinstance(v, dict | list) or _refused(v):
                        stack.append((v, (where, k), depth + 1))
                    if _refused(k):
                        stack.append((k, (where, k), None))
            else:
                for i in range(len(node) - 1, -1, -1):
                    v = node[i]
                    if isinstance(v, dict | list) or _refused(v):
                        stack.append((v, (where, i), depth + 1))
        elif isinstance(node, float) and not math.isfinite(node):
            return _path(root, where), (
                "is not a finite number: NaN, Infinity and -Infinity are not JSON "
                "(RFC 8259 § 6), and numbers must be within a double's range"
            )
        elif isinstance(node, str):
            char = _unstorable_char(node)
            if char is None:
                continue  # a bare str root that is fine
            if depth is None:
                return _path(root, where), f"has {char} in its key: {_why_not(char)}"
            kind = "" if char == "\\u0000" else ", a lone surrogate"
            return _path(root, where), f"contains {char}{kind}: {_why_not(char)}"
    return None


def _json_bytes(value: object) -> int | None:
    """*value*'s size as compact UTF-8 JSON, or None when too deep to encode.

    A lone surrogate counts as the 3 bytes UTF-8 would give it, were it allowed.
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except RecursionError:
        return None
    return len(encoded.encode("utf-8", "surrogatepass"))


def refuse_unstorable[T](value: T, info: ValidationInfo) -> T:
    """Reject *value* if a sink would refuse it, naming where (#31, #33).

    An ``AfterValidator`` for any JSON value: on a ``str`` field, text Postgres
    can store in ``text``. A length-constrained ``str`` refuses a lone
    surrogate before it, to count characters; a plain ``str`` takes one.

    Size first, in C: it bounds the walk, which is Python. A value too deep
    for ``json.dumps`` is refused by the walk, for its depth.
    """
    root = info.field_name or "value"
    size = _json_bytes(value)
    if size is not None and size > MAX_JSON_BYTES:
        raise TooLarge(
            f"{root} is {size} bytes as JSON, more than the {MAX_JSON_BYTES} co-status stores"
        )
    found = first_unstorable(value, root)
    if found is not None:
        raise ValueError(" ".join(found))
    return value


StorableJSONObject = Annotated[dict[str, Any], AfterValidator(refuse_unstorable)]
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
