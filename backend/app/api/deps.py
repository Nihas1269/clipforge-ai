"""Shared API dependencies: the auth cookie, the current user, email delivery
and the account-flow rate limiters."""

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.csrf import enforce_csrf
from app.core.errors import AppError
from app.core.rate_limit import InMemoryRateLimiter
from app.core.security import InvalidAccessTokenError, decode_access_token
from app.database import get_db
from app.models.user import User
from app.repositories.users import get_by_id
from app.services.email import EmailNotConfiguredError, EmailSender, build_email_sender

logger = logging.getLogger(__name__)

# Token requests (verification and reset emails) per account or address.
ACCOUNT_REQUEST_LIMIT = 5
ACCOUNT_REQUEST_WINDOW_SECONDS = 60 * 60
# Token redemptions and reset requests per client address.
CLIENT_ATTEMPT_LIMIT = 10
CLIENT_ATTEMPT_WINDOW_SECONDS = 15 * 60


@dataclass
class AccountRateLimiters:
    """Rate limiters of the email verification and password reset endpoints."""

    # Keyed by user id or email address.
    requests: InMemoryRateLimiter
    # Keyed by client address.
    attempts: InMemoryRateLimiter

    @classmethod
    def default(cls) -> "AccountRateLimiters":
        return cls(
            requests=InMemoryRateLimiter(
                ACCOUNT_REQUEST_LIMIT, ACCOUNT_REQUEST_WINDOW_SECONDS
            ),
            attempts=InMemoryRateLimiter(
                CLIENT_ATTEMPT_LIMIT, CLIENT_ATTEMPT_WINDOW_SECONDS
            ),
        )


def get_account_rate_limiters(request: Request) -> AccountRateLimiters:
    """Return the application's account-flow limiters (one set per app)."""
    limiters = getattr(request.app.state, "account_rate_limiters", None)
    if limiters is None:
        limiters = AccountRateLimiters.default()
        request.app.state.account_rate_limiters = limiters
    return limiters


def client_address(request: Request) -> str:
    """Return the client's address, used as a rate-limit key."""
    return request.client.host if request.client else "unknown"


def _resolve_email_sender(request: Request, settings: Settings) -> EmailSender:
    # Built on first use and kept on the app, so a missing or invalid email
    # configuration never stops the application from starting.
    sender = getattr(request.app.state, "email_sender", None)
    if sender is None:
        sender = build_email_sender(settings)
        request.app.state.email_sender = sender
    return sender


def get_email_sender(
    request: Request, settings: Settings = Depends(get_settings)
) -> EmailSender:
    """Return the configured email sender, or 503 if email is not configured."""
    try:
        return _resolve_email_sender(request, settings)
    except EmailNotConfiguredError as exc:
        logger.error("email_not_configured", extra={"reason": str(exc)})
        raise AppError(
            503,
            "email_unavailable",
            "Email delivery is not available. Try again later.",
        ) from None


def get_optional_email_sender(
    request: Request, settings: Settings = Depends(get_settings)
) -> EmailSender | None:
    """Return the configured email sender, or None if email is not configured."""
    try:
        return _resolve_email_sender(request, settings)
    except EmailNotConfiguredError as exc:
        logger.warning("email_not_configured", extra={"reason": str(exc)})
        return None


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


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """Return the current user if they are an admin; 403 otherwise.

    Unauthenticated requests still get 401 from get_current_user first.
    """
    if not user.is_admin:
        logger.info("admin_access_denied", extra={"user_id": str(user.id)})
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return user
