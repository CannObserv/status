"""The 422 for a request that fails validation, renderable whatever it carried.

FastAPI's own handler echoes each error's ``input``, and its ``JSONResponse``
refuses ``NaN`` and the infinities, and encodes as UTF-8, which refuses a lone
surrogate. Python's ``json.loads`` accepts all of them, so a body holding one
that failed validation anywhere was a 500 instead of its 422 (#31, #33). This
handler is FastAPI's, with those numbers spelled as strings, a lone surrogate
spelled as its escape, and without the input when it is nested too deep to
echo.
"""

import math

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.api.schemas.types import TooLarge

_SPELLED = {math.inf: "Infinity", -math.inf: "-Infinity"}


def _spell_surrogates(text: str) -> str:
    """*text* with each lone surrogate written as its escape: ``\\ud800``."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def json_safe(value: object) -> object:
    """*value* as strict JSON that UTF-8 encodes.

    A non-finite float becomes its JavaScript spelling, and a lone surrogate,
    in a string or a key, its escape. Recursive, as ``jsonable_encoder``
    before it is: the handler catches the ``RecursionError`` either raises.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else _SPELLED[value]
    if isinstance(value, str):
        return _spell_surrogates(value)
    if isinstance(value, dict):
        return {_spell_surrogates(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def _without_input(error: dict) -> dict:
    return {k: v for k, v in error.items() if k != "input"}


async def request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422, made strict JSON.

    ``json.loads`` takes nesting deeper than the encoders can recurse from
    here. Rather than let such an input make the 422 a 500, it answers the
    same errors without their ``input``.
    """
    # Refused for its size, an input echoed back would be as large again (#33).
    errors = [
        _without_input(error) if isinstance(error.get("ctx", {}).get("error"), TooLarge) else error
        for error in exc.errors()
    ]
    try:
        return JSONResponse(
            status_code=422, content={"detail": json_safe(jsonable_encoder(errors))}
        )
    except RecursionError:
        bare = [_without_input(error) for error in errors]
        return JSONResponse(status_code=422, content={"detail": json_safe(jsonable_encoder(bare))})
