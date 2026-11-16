from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import PASSWORD_MAX_LENGTH
from app.models.user import EMAIL_MAX_LENGTH


class LoginRequest(BaseModel):
    """Login request body.

    The email format is not validated here: an unknown or malformed email
    gets the same 401 response as a wrong password.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=EMAIL_MAX_LENGTH)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
