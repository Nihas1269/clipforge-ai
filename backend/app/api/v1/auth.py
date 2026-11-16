"""Registration, login, logout, email verification, and password reset.

Login returns a stateless JWT both in the body and in an HttpOnly cookie.
Logout only clears the cookie: a token that was already issued stays valid
until it expires (or until the user's token version changes, for example on a
password reset). See backend/README.md.

Email verification and password reset use the single-use tokens of
app/services/account_tokens.py. Raw tokens only ever leave the server inside
an email; they are never returned in a response or logged.
"""

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import (
    AccountRateLimiters,
    clear_access_token_cookie,
    client_address,
    csrf_protect,
    get_account_rate_limiters,
    get_current_user,
    get_email_sender,
    get_optional_email_sender,
    set_access_token_cookie,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.rate_limit import RateLimitExceededError
from app.core.security import create_access_token, verify_password, verify_password_dummy
from app.database import get_db
from app.models.user import User
from app.repositories.auth_tokens import InvalidOneTimeTokenError
from app.repositories.users import DuplicateEmailError, create_user, get_by_email
from app.schemas.auth import (
    EmailVerificationConfirm,
    LoginRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    TokenResponse,
)
from app.schemas.user import UserCreate, UserPublic
from app.services import account_tokens
from app.services.email import (
    EmailDeliveryError,
    EmailSender,
    OutboxEmailSender,
    deliver_all,
)

logger = logging.getLogger(__name__)

# The same message for an unknown email, a wrong password, and an inactive
# user, so the response never reveals whether an account exists.
INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password."

# The same response for every password reset request, whether or not the
# address has an account.
PASSWORD_RESET_REQUESTED_MESSAGE = (
    "If an account exists for this email address, a password reset link has "
    "been sent to it."
)
INVALID_TOKEN_MESSAGE = "The link is invalid or has expired."

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(csrf_protect)])


def _too_many_requests(exc: RateLimitExceededError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many attempts. Try again later.",
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


def _invalid_token() -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, "invalid_token", INVALID_TOKEN_MESSAGE)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserPublic,
)
def register(
    payload: UserCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    email_sender: EmailSender | None = Depends(get_optional_email_sender),
    limiters: AccountRateLimiters = Depends(get_account_rate_limiters),
    settings: Settings = Depends(get_settings),
) -> UserPublic:
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

    public_user = UserPublic.from_user(user)
    # Best effort: the account exists either way, and the user can ask for a
    # new verification email (POST /auth/verify-email/request).
    if email_sender is None:
        logger.warning(
            "email_verification_not_sent", extra={"user_id": str(user.id)}
        )
        return public_user
    outbox = OutboxEmailSender()
    try:
        account_tokens.send_email_verification(
            db,
            user,
            email_sender=outbox,
            rate_limiter=limiters.requests,
            settings=settings,
        )
    except Exception:  # noqa: BLE001 - registration already succeeded
        db.rollback()
        logger.exception(
            "email_verification_not_sent", extra={"user_id": str(user.id)}
        )
        return public_user
    background_tasks.add_task(deliver_all, email_sender, outbox.messages)
    return public_user


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


@router.post(
    "/verify-email/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageResponse,
)
def request_email_verification(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    email_sender: EmailSender = Depends(get_email_sender),
    limiters: AccountRateLimiters = Depends(get_account_rate_limiters),
    settings: Settings = Depends(get_settings),
) -> MessageResponse:
    """Email a new verification link to the current user.

    Sent synchronously so a delivery failure is reported (502), never
    answered as success. Earlier verification links stop working.
    """
    try:
        sent = account_tokens.send_email_verification(
            db,
            user,
            email_sender=email_sender,
            rate_limiter=limiters.requests,
            settings=settings,
        )
    except RateLimitExceededError as exc:
        logger.info(
            "email_verification_request_rate_limited", extra={"user_id": str(user.id)}
        )
        raise _too_many_requests(exc) from None
    except EmailDeliveryError:
        raise AppError(
            status.HTTP_502_BAD_GATEWAY,
            "email_delivery_failed",
            "The verification email could not be sent. Try again later.",
        ) from None

    if not sent:
        response.status_code = status.HTTP_200_OK
        return MessageResponse(message="Your email address is already verified.")
    logger.info("email_verification_sent", extra={"user_id": str(user.id)})
    return MessageResponse(message="A verification email has been sent.")


@router.post("/verify-email/confirm", response_model=MessageResponse)
def confirm_email_verification(
    payload: EmailVerificationConfirm,
    request: Request,
    db: Session = Depends(get_db),
    limiters: AccountRateLimiters = Depends(get_account_rate_limiters),
) -> MessageResponse:
    """Mark the token owner's email as verified.

    No login is needed: the emailed token proves ownership of the address.
    """
    try:
        account_tokens.verify_email(
            db,
            payload.token,
            rate_limiter=limiters.attempts,
            rate_limit_key=client_address(request),
        )
    except RateLimitExceededError as exc:
        logger.info("email_verification_rate_limited")
        raise _too_many_requests(exc) from None
    except InvalidOneTimeTokenError:
        logger.info("email_verification_rejected")
        raise _invalid_token() from None
    return MessageResponse(message="Your email address has been verified.")


@router.post(
    "/password-reset/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MessageResponse,
)
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    email_sender: EmailSender = Depends(get_email_sender),
    limiters: AccountRateLimiters = Depends(get_account_rate_limiters),
    settings: Settings = Depends(get_settings),
) -> MessageResponse:
    """Email a reset link if the address belongs to an active account.

    The response is the same for known, unknown, malformed and inactive
    addresses. The email is delivered after the response is sent, so neither
    delivery time nor delivery errors reveal whether the account exists;
    delivery failures are logged.
    """
    outbox = OutboxEmailSender()
    try:
        limiters.attempts.hit(f"password_reset_request_client:{client_address(request)}")
        account_tokens.request_password_reset(
            db,
            payload.email,
            email_sender=outbox,
            rate_limiter=limiters.requests,
            settings=settings,
        )
    except RateLimitExceededError as exc:
        logger.info("password_reset_request_rate_limited")
        raise _too_many_requests(exc) from None

    background_tasks.add_task(deliver_all, email_sender, outbox.messages)
    return MessageResponse(message=PASSWORD_RESET_REQUESTED_MESSAGE)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
def confirm_password_reset(
    payload: PasswordResetConfirm,
    request: Request,
    db: Session = Depends(get_db),
    limiters: AccountRateLimiters = Depends(get_account_rate_limiters),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Set a new password with a reset token.

    Every access token issued before the reset stops working (the user's
    token version is incremented), as do other outstanding reset links. The
    caller's auth cookie is cleared; the user logs in with the new password.
    """
    try:
        account_tokens.reset_password(
            db,
            payload.token,
            payload.new_password,
            rate_limiter=limiters.attempts,
            rate_limit_key=client_address(request),
        )
    except RateLimitExceededError as exc:
        logger.info("password_reset_rate_limited")
        raise _too_many_requests(exc) from None
    except InvalidOneTimeTokenError:
        logger.info("password_reset_rejected")
        raise _invalid_token() from None
    except ValueError:
        # Not reached after schema validation; kept so a password rule never
        # surfaces as a 500.
        raise AppError(
            422,
            "validation_error",
            "Request validation failed.",
        ) from None

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_access_token_cookie(response, settings)
    return response
