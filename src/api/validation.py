"""The 422 for a request that fails validation, renderable whatever it carried.

FastAPI's own handler echoes each error's ``input``, and its ``JSONResponse``
refuses ``NaN`` and the infinities. Python's ``json.loads`` accepts them, so a
body holding one that failed validation anywhere was a 500 instead of its 422
(#31). This handler is FastAPI's, with those numbers spelled as strings.
"""

import math

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_SPELLED = {math.inf: "Infinity", -math.inf: "-Infinity"}


def json_safe(value: object) -> object:
    """*value* with every non-finite float replaced by its JavaScript spelling.

    Recursive: it runs on ``jsonable_encoder``'s output, which has already
    recursed as deep.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else _SPELLED[value]
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


async def request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422, made strict JSON."""
    return JSONResponse(
        status_code=422, content={"detail": json_safe(jsonable_encoder(exc.errors()))}
    )
