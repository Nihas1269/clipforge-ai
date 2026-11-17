"""Subscriptions, payments and usage records (migration 0008).

Each user has at most one `Subscription` row: it is the user's current
subscription and is updated in place on renewal, cancellation or a plan
change. The history of what was paid lives in `Payment` rows, which are
never overwritten. Money is always an integer amount in minor currency units
(paise for INR).
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base

PLAN_CODE_MAX_LENGTH = 32
PROVIDER_MAX_LENGTH = 32
PROVIDER_ID_MAX_LENGTH = 255
REFERENCE_ID_MAX_LENGTH = 255


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum_values(members: type[enum.Enum]) -> list[str]:
    return [member.value for member in members]


def _to_utc(value: datetime | None) -> datetime | None:
    # Stored in UTC so comparisons also work on databases that keep only the
    # wall-clock time (SQLite).
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)


class SubscriptionStatus(str, enum.Enum):
    """Lifecycle state of a subscription.

    Only `active` and `cancelled` grant paid access, and only until
    `current_period_end` (a cancelled subscription keeps access for the
    period already paid for). See app/billing/service.py.
    """

    ACTIVE = "active"
    PENDING = "pending"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PaymentStatus(str, enum.Enum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class UsageMetric(str, enum.Enum):
    """What a usage record counts. Upload minutes are whole minutes."""

    UPLOAD_MINUTES = "upload_minutes"
    EXPORTS = "exports"


class Subscription(Base):
    """A user's current subscription (one row per user)."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "current_period_start IS NULL OR current_period_end IS NULL "
            "OR current_period_end > current_period_start",
            name="ck_subscriptions_period_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    plan_code: Mapped[str] = mapped_column(String(PLAN_CODE_MAX_LENGTH))
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(
            SubscriptionStatus,
            name="subscription_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    # For example "razorpay", or "manual" for a subscription granted by hand.
    provider: Mapped[str] = mapped_column(String(PROVIDER_MAX_LENGTH))
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(PROVIDER_ID_MAX_LENGTH), unique=True, index=True
    )
    # Half-open paid period [start, end), UTC. NULL until the provider
    # reports a period (for example while pending).
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    @validates("current_period_start", "current_period_end")
    def _validate_period(self, key: str, value: datetime | None) -> datetime | None:
        return _to_utc(value)

    def __repr__(self) -> str:
        status = (
            self.status.value
            if isinstance(self.status, SubscriptionStatus)
            else self.status
        )
        return (
            f"Subscription(id={self.id!r}, plan_code={self.plan_code!r}, "
            f"status={status!r})"
        )


class Payment(Base):
    """A payment reported by the payment provider."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="ck_payments_amount_non_negative"),
        CheckConstraint("length(currency) = 3", name="ck_payments_currency_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    # Unique: the same provider payment is never recorded twice.
    provider_payment_id: Mapped[str] = mapped_column(
        String(PROVIDER_ID_MAX_LENGTH), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"Payment(id={self.id!r})"


class UsageRecord(Base):
    """An append-only record of metered usage.

    `reference_id` identifies the operation that caused the usage (for
    example a video or export id); it is unique per user and metric, so
    recording the same operation again never counts it twice.
    """

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "metric",
            "reference_id",
            name="uq_usage_records_user_id_metric_reference_id",
        ),
        # Sums a user's usage of a metric over a period.
        Index(
            "ix_usage_records_user_id_metric_occurred_at",
            "user_id",
            "metric",
            "occurred_at",
        ),
        CheckConstraint("quantity >= 0", name="ck_usage_records_quantity_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    metric: Mapped[UsageMetric] = mapped_column(
        Enum(
            UsageMetric,
            name="usage_metric",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=_enum_values,
        )
    )
    quantity: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reference_id: Mapped[str] = mapped_column(String(REFERENCE_ID_MAX_LENGTH))

    @validates("occurred_at")
    def _validate_occurred_at(self, key: str, value: datetime) -> datetime | None:
        return _to_utc(value)

    def __repr__(self) -> str:
        metric = (
            self.metric.value if isinstance(self.metric, UsageMetric) else self.metric
        )
        return f"UsageRecord(id={self.id!r}, metric={metric!r})"
