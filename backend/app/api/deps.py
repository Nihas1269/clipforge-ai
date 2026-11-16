"""Shared API dependencies: the auth cookie and the current user."""

import logging

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.csrf import enforce_csrf
from app.core.security import InvalidAccessTokenError, decode_access_token
from app.database import get_db
from app.models.user import User
from app.repositories.users import get_by_id

logger = logging.getLogger(__name__)

ACCESS_TOKEN_COOKIE_NAME = "access_token"
# Sent on every API path; the cookie is never readable by scripts.
ACCESS_TOKEN_COOKIE_PATH = "/"
ACCESS_TOKEN_COOKIE_SAMESITE = "lax"

_bearer_scheme = HTTPBearer(auto_error=False)


def _cookie_is_secure(settings: Settings) -> bool:
    return settings.APP_ENV == "production"


def set_access_token_cookie(response: Response, token: str, settings: Settings) -> None:
    """Set the HttpOnly auth cookie; it expires together with the token."""
    response.set_cookie(
        ACCESS_TOKEN_COOKIE_NAME,
        token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path=ACCESS_TOKEN_COOKIE_PATH,
        httponly=True,
        samesite=ACCESS_TOKEN_COOKIE_SAMESITE,
        secure=_cookie_is_secure(settings),
    )


def clear_access_token_cookie(response: Response, settings: Settings) -> None:
    """Expire the auth cookie using the same attributes it was set with."""
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE_NAME,
        path=ACCESS_TOKEN_COOKIE_PATH,
        httponly=True,
        samesite=ACCESS_TOKEN_COOKIE_SAMESITE,
        secure=_cookie_is_secure(settings),
    )


def csrf_protect(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Reject state-changing requests from untrusted origins (see app/core/csrf.py)."""
    enforce_csrf(
        request,
        settings,
        has_auth_cookie=ACCESS_TOKEN_COOKIE_NAME in request.cookies,
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Not authenticated.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Return the active user identified by the access token.

    The token is read from `Authorization: Bearer` or, if that header is
    absent, from the `access_token` cookie. Cookie-authenticated
    state-changing requests must pass the CSRF origin check. Returns 401 for a
    missing, invalid, or expired token, an unknown or inactive user, and a
    token issued before the user's token version changed.
    """
    if credentials is not None:
        token = credentials.credentials
    else:
        token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
        if token:
            enforce_csrf(request, settings, has_auth_cookie=True)
    if not token:
        raise _unauthorized()

    try:
        payload = decode_access_token(token, settings=settings)
    except InvalidAccessTokenError:
        raise _unauthorized() from None

    user = get_by_id(db, payload.user_id)
    if (
        user is None
        or not user.is_active
        or user.token_version != payload.token_version
    ):
        logger.info("access_token_rejected", extra={"user_id": str(payload.user_id)})
        raise _unauthorized()
    return user
