"""Subscription checkout through the payment provider (Razorpay).

Every endpoint requires authentication (get_current_user, including the CSRF
check for cookie-authenticated requests) and acts only on the current user's
own subscription. While Razorpay is not configured every endpoint returns
503 billing_not_configured. See app/billing/checkout.py for the rules.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.billing.checkout import (
    BillingError,
    cancel_subscription,
    start_checkout,
    verify_checkout,
)
from app.billing.providers import (
    BillingNotConfiguredError,
    PaymentProvider,
    PaymentProviderError,
    PaymentProviderTimeoutError,
    build_payment_provider,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.database import get_db
from app.models.billing import SubscriptionStatus
from app.models.user import User
from app.schemas.billing import (
    CheckoutRequest,
    CheckoutResponse,
    SubscriptionRead,
    VerifyRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


def _not_configured(exc: BillingNotConfiguredError) -> AppError:
    # The reason names settings, never their values. A warning, not an
    # error: servers may run without billing on purpose.
    logger.warning("billing_not_configured", extra={"reason": str(exc)})
    return AppError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "billing_not_configured",
        "Billing is not available on this server.",
    )


def get_payment_provider(settings: Settings = Depends(get_settings)) -> PaymentProvider:
    """Return the configured payment provider, or 503 if it is not configured."""
    try:
        return build_payment_provider(settings)
    except BillingNotConfiguredError as exc:
        raise _not_configured(exc) from None


@contextmanager
def _billing_errors() -> Iterator[None]:
    try:
        yield
    except BillingError as exc:
        raise AppError(exc.status_code, exc.code, exc.message) from None
    except BillingNotConfiguredError as exc:
        raise _not_configured(exc) from None
    except PaymentProviderTimeoutError:
        raise AppError(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "payment_provider_timeout",
            "The payment provider did not respond in time. Try again.",
        ) from None
    except PaymentProviderError:
        raise AppError(
            status.HTTP_502_BAD_GATEWAY,
            "payment_provider_error",
            "The payment provider could not process the request. Try again later.",
        ) from None


@router.post("/checkout", response_model=CheckoutResponse)
def checkout(
    payload: CheckoutRequest,
    user: User = Depends(get_current_user),
    provider: PaymentProvider = Depends(get_payment_provider),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CheckoutResponse:
    """Start a checkout for a paid plan; the subscription stays `pending`."""
    with _billing_errors():
        session = start_checkout(
            db, user, payload.plan_code, provider, settings=settings
        )
    return CheckoutResponse(
        provider=session.provider,
        key_id=session.key_id,
        subscription_id=session.provider_subscription_id,
    )


@router.post(
    "/verify",
    response_model=SubscriptionRead,
    responses={202: {"description": "Payment not yet confirmed by the provider."}},
)
def verify(
    payload: VerifyRequest,
    response: Response,
    user: User = Depends(get_current_user),
    provider: PaymentProvider = Depends(get_payment_provider),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SubscriptionRead:
    """Confirm the checkout callback; 202 while the payment is still pending."""
    with _billing_errors():
        subscription = verify_checkout(
            db,
            user,
            provider,
            payment_id=payload.razorpay_payment_id,
            provider_subscription_id=payload.razorpay_subscription_id,
            signature=payload.razorpay_signature,
            settings=settings,
        )
    if subscription.status == SubscriptionStatus.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return SubscriptionRead.from_subscription(subscription)


@router.post("/cancel", response_model=SubscriptionRead)
def cancel(
    user: User = Depends(get_current_user),
    provider: PaymentProvider = Depends(get_payment_provider),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SubscriptionRead:
    """Cancel at the end of the current period; access lasts until then."""
    with _billing_errors():
        subscription = cancel_subscription(db, user, provider, settings=settings)
    return SubscriptionRead.from_subscription(subscription)
