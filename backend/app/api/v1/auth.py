"""Registration, login, and logout.

Login returns a stateless JWT both in the body and in an HttpOnly cookie.
Logout only clears the cookie: a token that was already issued stays valid
until it expires (or until the user's token version changes, for example on a
password reset). See backend/README.md.
"""

import logging

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import clear_access_token_cookie, csrf_protect, set_access_token_cookie
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import create_access_token, verify_password, verify_password_dummy
from app.database import get_db
from app.repositories.users import DuplicateEmailError, create_user, get_by_email
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserCreate, UserPublic

logger = logging.getLogger(__name__)

# The same message for an unknown email, a wrong password, and an inactive
# user, so the response never reveals whether an account exists.
INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password."

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(csrf_protect)])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserPublic,
)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> UserPublic:
    try:
        user = create_user(
            db,
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
        )
    except DuplicateEmailError:
        logger.info("registration_rejected_duplicate_email")
        raise AppError(
            status.HTTP_409_CONFLICT,
            "email_already_registered",
            "A user with this email already exists.",
        ) from None
    except ValueError:
        # Not reached after schema validation; kept so a repository rule
        # never surfaces as a 500.
        raise AppError(
            422,
            "validation_error",
            "Request validation failed.",
        ) from None
    return UserPublic.from_user(user)


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = get_by_email(db, payload.email)
    if user is None:
        # Hash anyway so the response time does not reveal unknown emails.
        verify_password_dummy(payload.password)
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.hashed_password)

    if user is None or not password_ok or not user.is_active:
        logger.info("login_failed")
        raise AppError(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_credentials",
            INVALID_CREDENTIALS_MESSAGE,
        )

    token = create_access_token(
        user.id, token_version=user.token_version, settings=settings
    )
    set_access_token_cookie(response, token, settings)
    # Token responses must not be cached (RFC 6749, section 5.1).
    response.headers["Cache-Control"] = "no-store"
    logger.info("login_succeeded", extra={"user_id": str(user.id)})
    return TokenResponse(access_token=token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(settings: Settings = Depends(get_settings)) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_access_token_cookie(response, settings)
    return response
