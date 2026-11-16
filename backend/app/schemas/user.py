import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from app.models.user import EMAIL_MAX_LENGTH

FULL_NAME_MAX_LENGTH = 255


class UserCreate(BaseModel):
    """Registration request body."""

    model_config = ConfigDict(extra="forbid")

    # email-validator also enforces the RFC length limits.
    email: EmailStr
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH
    )
    full_name: str | None = Field(default=None, max_length=FULL_NAME_MAX_LENGTH)

    @field_validator("email", mode="before")
    @classmethod
    def _strip_email(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if len(value) > EMAIL_MAX_LENGTH:
                raise ValueError("Email is too long.")
            # Only ASCII addresses: they are lowercased identically by Python
            # and every database, which keeps the lowercase uniqueness rule
            # consistent.
            if not value.isascii():
                raise ValueError("Email must contain only ASCII characters.")
        return value

    @field_validator("email", mode="after")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        # Same normalization as the users table (stored lowercase, unique).
        return value.lower()

    @field_validator("full_name", mode="after")
    @classmethod
    def _blank_full_name_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class UserUpdate(BaseModel):
    """Profile update body. Only `full_name` can be changed by the user.

    Unknown fields (including `email`, `is_admin`, `is_active`) are rejected.
    An omitted `full_name` leaves it unchanged; null or blank clears it.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=FULL_NAME_MAX_LENGTH)

    @field_validator("full_name", mode="after")
    @classmethod
    def _blank_full_name_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class PasswordChange(BaseModel):
    """Password change body. The new password follows the registration rules."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(
        min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH
    )


class UserPublic(BaseModel):
    """A user as returned by the API. Never includes the password hash."""

    id: uuid.UUID
    email: str
    full_name: str | None
    is_active: bool
    email_verified: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: Any) -> "UserPublic":
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            email_verified=user.email_verified_at is not None,
            created_at=user.created_at,
        )
