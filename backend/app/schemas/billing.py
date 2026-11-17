from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.billing.plans import PlanCode
from app.billing.providers.razorpay import SIGNATURE_MAX_LENGTH
from app.billing.service import as_utc

# Razorpay ids such as "pay_..." and "sub_...".
PROVIDER_ID_REGEX = r"^[A-Za-z0-9_]{1,64}$"


class CheckoutRequest(BaseModel):
    """Checkout body. Only the plan is chosen by the client; the price never is."""

    model_config = ConfigDict(extra="forbid")

    plan_code: PlanCode


class CheckoutResponse(BaseModel):
    """What the frontend passes to Razorpay Checkout."""

    provider: Literal["razorpay"]
    key_id: str
    subscription_id: str


class VerifyRequest(BaseModel):
    """The fields Razorpay Checkout returns to the browser after payment."""

    model_config = ConfigDict(extra="forbid")

    razorpay_payment_id: str = Field(pattern=PROVIDER_ID_REGEX)
    razorpay_subscription_id: str = Field(pattern=PROVIDER_ID_REGEX)
    razorpay_signature: str = Field(min_length=1, max_length=SIGNATURE_MAX_LENGTH)


class SubscriptionRead(BaseModel):
    """The user's subscription. `cancelled` keeps access until the period end."""

    plan_code: str
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None

    @classmethod
    def from_subscription(cls, subscription: Any) -> "SubscriptionRead":
        status = subscription.status
        start = subscription.current_period_start
        end = subscription.current_period_end
        return cls(
            plan_code=subscription.plan_code,
            status=getattr(status, "value", status),
            current_period_start=as_utc(start) if start is not None else None,
            current_period_end=as_utc(end) if end is not None else None,
        )
