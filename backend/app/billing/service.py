"""Effective plan, usage accounting and payment records.

Paid access: a subscription grants its plan only while its status is
`active` or `cancelled` (cancellation keeps access for the period already
paid for) **and** `current_period_start <= now < current_period_end`. Every
other status (`pending`, `past_due`, `expired`), a missing or ended period, a
free or unknown `plan_code`, or no subscription at all gives the free plan.

Usage periods are half-open UTC intervals `[start, end)`: the subscription's
current period for a user with paid access, otherwise the calendar month in
UTC. All timestamps are timezone-aware UTC; naive values read back from
databases that do not store a time zone (SQLite) are treated as UTC.

Rounding policy: upload usage is counted in whole minutes, rounded **up** per
upload (`upload_minutes_from_seconds`), so 61 seconds count as 2 minutes.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing.plans import CURRENCY_INR, Plan, PlanCode, get_plans
from app.core.config import Settings
from app.models.billing import (
    PROVIDER_ID_MAX_LENGTH,
    REFERENCE_ID_MAX_LENGTH,
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    UsageMetric,
    UsageRecord,
)
from app.models.user import User

logger = logging.getLogger(__name__)

# The only subscription states that can grant paid access (within the period).
PAID_ACCESS_STATUSES = frozenset(
    {SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED}
)

SECONDS_PER_MINUTE = 60


class DuplicatePaymentError(Exception):
    """Raised when a payment with the same provider_payment_id already exists."""


class UsageConflictError(Exception):
    """Raised when a reference_id is reused with a different quantity."""


@dataclass(frozen=True)
class UsageSummary:
    """A user's usage in the current period [period_start, period_end)."""

    plan: Plan
    period_start: datetime
    period_end: datetime
    upload_minutes: int
    exports: int


def as_utc(value: datetime) -> datetime:
    """Return `value` in UTC; a naive value is taken to be UTC already."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return now.astimezone(timezone.utc)


def calendar_month_period(now: datetime) -> tuple[datetime, datetime]:
    """Return the UTC calendar month containing `now` as [start, end)."""
    now = _now(now)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return start, end


def get_subscription(db: Session, user: User) -> Subscription | None:
    """Return the user's subscription row (at most one), whatever its status."""
    return db.scalars(
        select(Subscription).where(Subscription.user_id == user.id)
    ).one_or_none()


def grants_paid_access(subscription: Subscription, now: datetime) -> bool:
    """Return True if the subscription's status and period grant access now."""
    now = _now(now)
    if subscription.status not in PAID_ACCESS_STATUSES:
        return False
    if (
        subscription.current_period_start is None
        or subscription.current_period_end is None
    ):
        return False
    start = as_utc(subscription.current_period_start)
    end = as_utc(subscription.current_period_end)
    return start <= now < end


def _resolve(
    db: Session, user: User, now: datetime, settings: Settings | None
) -> tuple[Plan, Subscription | None]:
    """Return the effective plan and, for a paid plan, the granting subscription."""
    plans = get_plans(settings)
    free = plans[PlanCode.FREE]
    subscription = get_subscription(db, user)
    if subscription is None or not grants_paid_access(subscription, now):
        return free, None
    try:
        code = PlanCode(subscription.plan_code)
    except ValueError:
        # Never grant access for a plan this code does not know.
        logger.warning(
            "subscription_unknown_plan",
            extra={
                "user_id": str(user.id),
                "subscription_id": str(subscription.id),
                "plan_code": subscription.plan_code[:32],
            },
        )
        return free, None
    plan = plans[code]
    if not plan.is_paid:
        return free, None
    return plan, subscription


def get_effective_plan(
    db: Session,
    user: User,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Plan:
    """Return the plan the user is entitled to now.

    `free` unless the user's subscription grants paid access (see the module
    docstring for exactly when it does).
    """
    plan, _ = _resolve(db, user, _now(now), settings)
    return plan


def get_current_period(
    db: Session,
    user: User,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> tuple[Plan, datetime, datetime]:
    """Return the effective plan and the current usage period [start, end)."""
    now = _now(now)
    plan, subscription = _resolve(db, user, now, settings)
    if subscription is not None:
        # grants_paid_access() checked that both ends are set.
        assert subscription.current_period_start is not None
        assert subscription.current_period_end is not None
        return (
            plan,
            as_utc(subscription.current_period_start),
            as_utc(subscription.current_period_end),
        )
    start, end = calendar_month_period(now)
    return plan, start, end


def get_usage_for_current_period(
    db: Session,
    user: User,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> UsageSummary:
    """Return the user's usage totals for the current period."""
    plan, start, end = get_current_period(db, user, now=now, settings=settings)
    rows = db.execute(
        select(UsageRecord.metric, func.coalesce(func.sum(UsageRecord.quantity), 0))
        .where(
            UsageRecord.user_id == user.id,
            UsageRecord.occurred_at >= start,
            UsageRecord.occurred_at < end,
        )
        .group_by(UsageRecord.metric)
    ).all()
    totals = {UsageMetric(metric): int(total) for metric, total in rows}
    return UsageSummary(
        plan=plan,
        period_start=start,
        period_end=end,
        upload_minutes=totals.get(UsageMetric.UPLOAD_MINUTES, 0),
        exports=totals.get(UsageMetric.EXPORTS, 0),
    )


def upload_minutes_from_seconds(seconds: float) -> int:
    """Return the billable whole minutes for a duration, rounded up.

    The duration is first rounded to whole milliseconds so floating-point
    noise (such as 120.0000000001) does not add a minute. 0 seconds is 0
    minutes. Raises ValueError for negative or non-finite values.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("Duration must be a number of seconds.")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Duration must be a finite, non-negative number.")
    milliseconds = round(seconds * 1000)
    return -(-milliseconds // (SECONDS_PER_MINUTE * 1000))


def _clean_identifier(value: str, name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    value = value.strip()
    if not value or len(value) > max_length:
        raise ValueError(f"{name} must be 1-{max_length} characters.")
    if not value.isprintable():
        raise ValueError(f"{name} must not contain control characters.")
    return value


def _check_quantity(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")


def _find_usage(
    db: Session, user_id: uuid.UUID, metric: UsageMetric, reference_id: str
) -> UsageRecord | None:
    return db.scalars(
        select(UsageRecord).where(
            UsageRecord.user_id == user_id,
            UsageRecord.metric == metric,
            UsageRecord.reference_id == reference_id,
        )
    ).one_or_none()


def _existing_usage(record: UsageRecord, quantity: int) -> UsageRecord:
    if record.quantity != quantity:
        raise UsageConflictError(
            "This reference_id was already recorded with a different quantity."
        )
    logger.info(
        "usage_record_duplicate",
        extra={
            "user_id": str(record.user_id),
            "usage_record_id": str(record.id),
            "metric": UsageMetric(record.metric).value,
        },
    )
    return record


def record_usage(
    db: Session,
    user: User,
    metric: UsageMetric | str,
    quantity: int,
    *,
    reference_id: str,
    occurred_at: datetime | None = None,
) -> UsageRecord:
    """Record usage once per (user, metric, reference_id) and commit it.

    Idempotent: recording the same reference again returns the existing
    record (a different quantity raises UsageConflictError). Concurrent calls
    are safe because the database's unique constraint decides which insert
    wins; the loser returns the winner's record. `quantity` must be a
    non-negative integer; convert upload durations with
    upload_minutes_from_seconds(). Raises ValueError for invalid input.

    The session is committed, or rolled back on a conflict, so the caller
    must not have other pending work in it.
    """
    metric = UsageMetric(metric)
    _check_quantity(quantity, "quantity")
    reference_id = _clean_identifier(
        reference_id, "reference_id", REFERENCE_ID_MAX_LENGTH
    )
    occurred_at = _now(occurred_at)

    existing = _find_usage(db, user.id, metric, reference_id)
    if existing is not None:
        return _existing_usage(existing, quantity)

    record = UsageRecord(
        user_id=user.id,
        metric=metric,
        quantity=quantity,
        occurred_at=occurred_at,
        reference_id=reference_id,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent call recorded the same reference first.
        db.rollback()
        existing = _find_usage(db, user.id, metric, reference_id)
        if existing is None:
            raise
        return _existing_usage(existing, quantity)

    logger.info(
        "usage_recorded",
        extra={
            "user_id": str(user.id),
            "usage_record_id": str(record.id),
            "metric": metric.value,
            "quantity": quantity,
        },
    )
    return record


def record_payment(
    db: Session,
    user: User,
    *,
    amount_minor: int,
    provider_payment_id: str,
    status: PaymentStatus | str,
    currency: str = CURRENCY_INR,
    subscription: Subscription | None = None,
) -> Payment:
    """Store and commit a payment reported by the provider.

    `amount_minor` is an integer in minor units (paise). Raises
    DuplicatePaymentError if `provider_payment_id` was already recorded (also
    when a concurrent call wins the race; the database's unique index
    decides), and ValueError for invalid input or a subscription of another
    user. The session is committed, or rolled back on a duplicate.
    """
    status = PaymentStatus(status)
    _check_quantity(amount_minor, "amount_minor")
    provider_payment_id = _clean_identifier(
        provider_payment_id, "provider_payment_id", PROVIDER_ID_MAX_LENGTH
    )
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValueError("currency must be a three-letter upper-case ISO 4217 code.")
    if subscription is not None and subscription.user_id != user.id:
        raise ValueError("The subscription belongs to another user.")

    existing = db.scalars(
        select(Payment.id).where(Payment.provider_payment_id == provider_payment_id)
    ).first()
    if existing is not None:
        raise DuplicatePaymentError("This payment was already recorded.")

    payment = Payment(
        user_id=user.id,
        subscription_id=subscription.id if subscription is not None else None,
        amount_minor=amount_minor,
        currency=currency,
        status=status,
        provider_payment_id=provider_payment_id,
    )
    db.add(payment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        duplicate = db.scalars(
            select(Payment.id).where(Payment.provider_payment_id == provider_payment_id)
        ).first()
        if duplicate is None:
            raise
        raise DuplicatePaymentError("This payment was already recorded.") from exc

    logger.info(
        "payment_recorded",
        extra={
            "user_id": str(user.id),
            "payment_id": str(payment.id),
            "status": status.value,
            "amount_minor": amount_minor,
            "currency": currency,
        },
    )
    return payment
