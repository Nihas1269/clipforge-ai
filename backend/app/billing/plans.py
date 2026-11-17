"""Code-defined billing plans.

There are two plans, `free` and `pro`. Their structure lives here; their
price, included credits and limits come from the settings (`BILLING_*`), so
none of the values are final commercial terms. The Razorpay plan id of the pro
plan is the `RAZORPAY_PRO_PLAN_ID` setting and is never hardcoded.

Settings validate the values when the application starts (see
app/core/config.py); `Plan` and `PlanLimits` check them again when built.
"""

import enum
from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings, get_settings

CURRENCY_INR = "INR"

PlanInterval = Literal["month"]


class PlanCode(str, enum.Enum):
    """Identifier of a plan, stored in `subscriptions.plan_code`."""

    FREE = "free"
    PRO = "pro"


class InvalidPlanConfigurationError(ValueError):
    """Raised when a plan is built from invalid values."""


@dataclass(frozen=True)
class PlanLimits:
    """Usage and size limits of a plan.

    Monthly limits apply per usage period (see app/billing/service.py) and may
    be 0. Size and duration limits must be positive.
    """

    monthly_upload_minutes: int
    monthly_exports: int
    max_video_duration_seconds: int
    max_upload_size_mb: int

    def __post_init__(self) -> None:
        for name in ("monthly_upload_minutes", "monthly_exports"):
            _check_int(name, getattr(self, name), minimum=0)
        for name in ("max_video_duration_seconds", "max_upload_size_mb"):
            _check_int(name, getattr(self, name), minimum=1)


@dataclass(frozen=True)
class Plan:
    """A billing plan. `price_minor` is in minor currency units (paise)."""

    code: PlanCode
    name: str
    price_minor: int
    currency: str
    interval: PlanInterval
    limits: PlanLimits
    # Usage credits included per billing period (credit ledger: CF-049).
    included_credits: int = 0
    # The payment provider's plan id; None for the free plan or when unset.
    razorpay_plan_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, PlanCode):
            raise InvalidPlanConfigurationError("Plan code must be a PlanCode.")
        if not self.name.strip():
            raise InvalidPlanConfigurationError("Plan name must not be empty.")
        _check_int("price_minor", self.price_minor, minimum=0)
        _check_int("included_credits", self.included_credits, minimum=0)
        if len(self.currency) != 3 or not self.currency.isupper():
            raise InvalidPlanConfigurationError(
                "Currency must be a three-letter ISO 4217 code."
            )
        if self.interval != "month":
            raise InvalidPlanConfigurationError("Plan interval must be 'month'.")

    @property
    def is_paid(self) -> bool:
        return self.price_minor > 0


def _check_int(name: str, value: object, *, minimum: int) -> None:
    # bool is a subclass of int but never a valid amount.
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidPlanConfigurationError(
            f"{name} must be an integer of at least {minimum}."
        )


def get_plans(settings: Settings | None = None) -> dict[PlanCode, Plan]:
    """Return every plan, built from the settings."""
    if settings is None:
        settings = get_settings()
    return {
        PlanCode.FREE: Plan(
            code=PlanCode.FREE,
            name="Free",
            price_minor=0,
            currency=CURRENCY_INR,
            interval="month",
            limits=PlanLimits(
                monthly_upload_minutes=settings.BILLING_FREE_MONTHLY_UPLOAD_MINUTES,
                monthly_exports=settings.BILLING_FREE_MONTHLY_EXPORTS,
                max_video_duration_seconds=settings.BILLING_FREE_MAX_VIDEO_DURATION_SECONDS,
                max_upload_size_mb=settings.BILLING_FREE_MAX_UPLOAD_SIZE_MB,
            ),
        ),
        PlanCode.PRO: Plan(
            code=PlanCode.PRO,
            name="Pro",
            price_minor=settings.BILLING_PRO_PRICE_MINOR,
            currency=CURRENCY_INR,
            interval="month",
            limits=PlanLimits(
                monthly_upload_minutes=settings.BILLING_PRO_MONTHLY_UPLOAD_MINUTES,
                monthly_exports=settings.BILLING_PRO_MONTHLY_EXPORTS,
                max_video_duration_seconds=settings.BILLING_PRO_MAX_VIDEO_DURATION_SECONDS,
                max_upload_size_mb=settings.BILLING_PRO_MAX_UPLOAD_SIZE_MB,
            ),
            included_credits=settings.BILLING_PRO_INCLUDED_CREDITS,
            razorpay_plan_id=settings.RAZORPAY_PRO_PLAN_ID,
        ),
    }


def get_plan(code: PlanCode | str, settings: Settings | None = None) -> Plan:
    """Return the plan with the code. Raises ValueError for an unknown code."""
    return get_plans(settings)[PlanCode(code)]
