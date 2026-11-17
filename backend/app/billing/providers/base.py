"""The payment provider interface used by the billing endpoints.

A provider creates and cancels subscriptions at the payment service, reads
their state back, and checks the two kinds of signatures the service issues:

- the **payment signature** returned to the browser by the checkout widget,
  signed with the API key secret, and
- the **webhook signature** of a server-to-server notification, signed with
  the separate webhook secret.

A signature alone never grants paid access: the caller must also confirm the
subscription's state with `fetch_subscription` (or a verified webhook).

Errors never contain secrets, request payloads or response bodies.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.billing.plans import Plan
from app.models.user import User


class BillingNotConfiguredError(Exception):
    """Raised when the provider's settings are missing or invalid.

    The message names the settings, never their values.
    """


class PaymentProviderError(Exception):
    """Raised when a call to the provider fails or its answer is unusable.

    `status_code` is the provider's HTTP status when there was one.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PaymentProviderTimeoutError(PaymentProviderError):
    """Raised when the provider does not answer within the timeout."""


@dataclass(frozen=True)
class CheckoutSession:
    """What the frontend needs to open the provider's checkout widget.

    `key_id` is the provider's public key id, never the secret.
    """

    provider: str
    key_id: str
    provider_subscription_id: str


@dataclass(frozen=True)
class ProviderSubscription:
    """A subscription as reported by the provider.

    `status` is the provider's own status string (lower case). The period is
    in UTC and None while the provider reports none.
    """

    provider_subscription_id: str
    status: str
    plan_id: str | None
    current_start: datetime | None
    current_end: datetime | None


class PaymentProvider(Protocol):
    """A payment service that sells subscriptions."""

    # Stored in `subscriptions.provider`, e.g. "razorpay".
    name: str
    # Public key id the checkout widget is opened with.
    key_id: str

    def create_subscription(self, user: User, plan: Plan) -> CheckoutSession:
        """Create a subscription for the user and paid plan at the provider."""
        ...

    def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        """Return the provider's current state of the subscription."""
        ...

    def cancel_subscription(
        self, provider_subscription_id: str, *, at_cycle_end: bool = True
    ) -> ProviderSubscription:
        """Cancel the subscription at the end of its current billing period.

        With `at_cycle_end=False` it is cancelled at once (used for an
        abandoned checkout that was never paid for locally).
        """
        ...

    def verify_payment_signature(
        self, *, payment_id: str, subscription_id: str, signature: str
    ) -> bool:
        """Return True if the checkout callback's signature is valid."""
        ...

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        """Return True if the webhook body's signature is valid."""
        ...
