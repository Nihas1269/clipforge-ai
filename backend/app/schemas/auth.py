from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import (
    ONE_TIME_TOKEN_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
)
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


class MessageResponse(BaseModel):
    message: str


class EmailVerificationConfirm(BaseModel):
    """Body of the email verification confirmation."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=ONE_TIME_TOKEN_MAX_LENGTH)


class PasswordResetRequest(BaseModel):
    """Body of a password reset request.

    The email format is not validated here: a malformed address gets the same
    response as an unknown one, so the response never reveals whether an
    account exists.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=EMAIL_MAX_LENGTH)


class PasswordResetConfirm(BaseModel):
    """Body of a password reset confirmation. The password follows the registration rules."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=ONE_TIME_TOKEN_MAX_LENGTH)
    new_password: str = Field(
        min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH
    )
