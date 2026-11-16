"""The current user's profile and password.

Every endpoint requires authentication through get_current_user, which also
applies the CSRF origin check to cookie-authenticated PATCH/POST requests.
Only `full_name` and the password can be changed here; `email`, `is_admin`
and `is_active` are not accepted.
"""

import logging

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, set_access_token_cookie
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import create_access_token, verify_password
from app.database import get_db
from app.models.user import User
from app.repositories.users import set_password, update_full_name
from app.schemas.auth import TokenResponse
from app.schemas.user import PasswordChange, UserPublic, UserUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserPublic)
def read_me(user: User = Depends(get_current_user)) -> UserPublic:
    return UserPublic.from_user(user)


@router.patch("/me", response_model=UserPublic)
def update_me(
    payload: UserUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPublic:
    if "full_name" in payload.model_fields_set:
        update_full_name(db, user, payload.full_name)
    return UserPublic.from_user(user)


@router.post("/me/password", response_model=TokenResponse)
def change_password(
    payload: PasswordChange,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """Change the password and return a fresh access token.

    The change invalidates every previously issued token (including the one
    used for this request), so a new token is returned and set as the cookie.
    """
    if not verify_password(payload.current_password, user.hashed_password):
        logger.info("password_change_rejected", extra={"user_id": str(user.id)})
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_current_password",
            "The current password is incorrect.",
        )

    set_password(db, user, payload.new_password)
    token = create_access_token(
        user.id, token_version=user.token_version, settings=settings
    )
    set_access_token_cookie(response, token, settings)
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(access_token=token)
