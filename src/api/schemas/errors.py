"""Pydantic schemas for error responses.

Declaring auth failures with a concrete model rather than a bare description
matters for the generated SDK: an undescribed response makes
``datamodel-code-generator`` widen every operation's return type to ``Any``,
which silently disables type checking for consumers on the success path too.
"""

from pydantic import BaseModel


class AuthErrorDetail(BaseModel):
    """FastAPI's ``HTTPException`` body for the authentication failures."""

    detail: str
