"""Payment providers (see base.py for the interface)."""

from app.billing.providers.base import (
    BillingNotConfiguredError,
    CheckoutSession,
    PaymentProvider,
    PaymentProviderError,
    PaymentProviderTimeoutError,
    ProviderSubscription,
)
from app.billing.providers.razorpay import RazorpayProvider
from app.core.config import Settings

__all__ = [
    "BillingNotConfiguredError",
    "CheckoutSession",
    "PaymentProvider",
    "PaymentProviderError",
    "PaymentProviderTimeoutError",
    "ProviderSubscription",
    "RazorpayProvider",
    "build_payment_provider",
]


def build_payment_provider(settings: Settings) -> PaymentProvider:
    """Return the configured payment provider (Razorpay is the only one).

    Raises BillingNotConfiguredError when its settings are missing or invalid.
    """
    return RazorpayProvider.from_settings(settings)
