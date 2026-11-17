import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.billing import service as billing_service
from app.billing.plans import (
    InvalidPlanConfigurationError,
    Plan,
    PlanCode,
    PlanLimits,
    get_plan,
    get_plans,
)
from app.billing.service import (
    PAID_ACCESS_STATUSES,
    DuplicatePaymentError,
    UsageConflictError,
    calendar_month_period,
    get_current_period,
    get_effective_plan,
    get_usage_for_current_period,
    record_payment,
    record_usage,
    upload_minutes_from_seconds,
)
from app.core.config import Settings
from app.database import Base, create_db_engine, create_session_factory
from app.models.billing import (
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    UsageMetric,
    UsageRecord,
)
from app.models.user import User

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    """Session on an isolated in-memory database with the full schema."""
    engine = create_db_engine(
        SQLITE_IN_MEMORY_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = create_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_user(db, email="alice@example.com") -> User:
    user = User(email=email, hashed_password="x")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def user(db):
    return _make_user(db)


def _subscribe(
    db,
    user,
    *,
    status=SubscriptionStatus.ACTIVE,
    plan_code="pro",
    start=NOW - timedelta(days=10),
    end=NOW + timedelta(days=20),
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        plan_code=plan_code,
        status=status,
        provider="razorpay",
        provider_subscription_id=f"sub_{uuid.uuid4().hex[:14]}",
        current_period_start=start,
        current_period_end=end,
    )
    db.add(subscription)
    db.commit()
    return subscription


def _usage_count(db) -> int:
    return db.scalar(select(func.count()).select_from(UsageRecord))


# --- Plans and settings -----------------------------------------------------


def test_plans_have_the_required_fields_and_defaults():
    plans = get_plans(Settings())

    assert set(plans) == {PlanCode.FREE, PlanCode.PRO}
    free, pro = plans[PlanCode.FREE], plans[PlanCode.PRO]
    assert free.code == PlanCode.FREE
    assert free.price_minor == 0
    assert not free.is_paid
    assert pro.is_paid
    assert pro.price_minor == 99900
    for plan in (free, pro):
        assert plan.currency == "INR"
        assert plan.interval == "month"
        assert isinstance(plan.price_minor, int)
    assert free.limits == PlanLimits(
        monthly_upload_minutes=60,
        monthly_exports=10,
        max_video_duration_seconds=1800,
        max_upload_size_mb=250,
    )
    assert pro.limits == PlanLimits(
        monthly_upload_minutes=600,
        monthly_exports=200,
        max_video_duration_seconds=7200,
        max_upload_size_mb=500,
    )
    # No hardcoded provider plan id.
    assert free.razorpay_plan_id is None
    assert pro.razorpay_plan_id is None


def test_plan_limits_price_and_plan_id_are_overridable_from_settings(monkeypatch):
    monkeypatch.setenv("BILLING_FREE_MONTHLY_UPLOAD_MINUTES", "15")
    monkeypatch.setenv("BILLING_FREE_MONTHLY_EXPORTS", "0")
    monkeypatch.setenv("BILLING_FREE_MAX_VIDEO_DURATION_SECONDS", "600")
    monkeypatch.setenv("BILLING_FREE_MAX_UPLOAD_SIZE_MB", "100")
    monkeypatch.setenv("BILLING_PRO_PRICE_MINOR", "249900")
    monkeypatch.setenv("BILLING_PRO_INCLUDED_CREDITS", "1000")
    monkeypatch.setenv("BILLING_PRO_MONTHLY_UPLOAD_MINUTES", "900")
    monkeypatch.setenv("BILLING_PRO_MONTHLY_EXPORTS", "300")
    monkeypatch.setenv("BILLING_PRO_MAX_VIDEO_DURATION_SECONDS", "10800")
    monkeypatch.setenv("BILLING_PRO_MAX_UPLOAD_SIZE_MB", "1024")
    monkeypatch.setenv("RAZORPAY_PRO_PLAN_ID", " plan_TEST0000000001 ")

    free = get_plan("free")
    pro = get_plan(PlanCode.PRO)

    assert free.limits == PlanLimits(15, 0, 600, 100)
    assert pro.limits == PlanLimits(900, 300, 10800, 1024)
    assert pro.price_minor == 249900
    assert pro.included_credits == 1000
    assert pro.razorpay_plan_id == "plan_TEST0000000001"


def test_blank_razorpay_plan_id_is_unset():
    assert Settings(RAZORPAY_PRO_PLAN_ID="  ").RAZORPAY_PRO_PLAN_ID is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"BILLING_FREE_MONTHLY_EXPORTS": -1},
        {"BILLING_FREE_MAX_UPLOAD_SIZE_MB": 0},
        {"BILLING_PRO_MAX_VIDEO_DURATION_SECONDS": 0},
        {"BILLING_PRO_PRICE_MINOR": 0},
        {"BILLING_PRO_PRICE_MINOR": "999.50"},
        {"BILLING_PRO_INCLUDED_CREDITS": -5},
        # The pro plan must not offer less than the free plan.
        {"BILLING_PRO_MONTHLY_EXPORTS": 5},
        {"BILLING_FREE_MONTHLY_UPLOAD_MINUTES": 601},
        {"RAZORPAY_PRO_PLAN_ID": "plan/../x"},
        {"RAZORPAY_PRO_PLAN_ID": "p" * 65},
    ],
)
def test_invalid_plan_configuration_is_rejected_at_startup(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_plan_objects_validate_their_values():
    limits = PlanLimits(1, 1, 1, 1)
    with pytest.raises(InvalidPlanConfigurationError):
        PlanLimits(-1, 1, 1, 1)
    with pytest.raises(InvalidPlanConfigurationError):
        PlanLimits(1, 1, 0, 1)
    with pytest.raises(InvalidPlanConfigurationError):
        PlanLimits(1, True, 1, 1)
    with pytest.raises(InvalidPlanConfigurationError):
        Plan(PlanCode.PRO, "Pro", -1, "INR", "month", limits)
    with pytest.raises(InvalidPlanConfigurationError):
        Plan(PlanCode.PRO, "Pro", 100, "inr", "month", limits)
    with pytest.raises(InvalidPlanConfigurationError):
        Plan(PlanCode.PRO, " ", 100, "INR", "month", limits)
    with pytest.raises(InvalidPlanConfigurationError):
        Plan(PlanCode.PRO, "Pro", 100, "INR", "year", limits)  # type: ignore[arg-type]


def test_get_plan_rejects_unknown_code():
    with pytest.raises(ValueError):
        get_plan("enterprise")


# --- Effective plan -----------------------------------------------------------


def test_default_plan_is_free(db, user):
    assert get_effective_plan(db, user, now=NOW).code == PlanCode.FREE


def test_active_unexpired_subscription_returns_pro(db, user):
    _subscribe(db, user)

    plan = get_effective_plan(db, user, now=NOW)

    assert plan.code == PlanCode.PRO


def test_expired_subscription_falls_back_to_free(db, user):
    # Still marked active, but the period has ended.
    _subscribe(db, user, start=NOW - timedelta(days=40), end=NOW - timedelta(seconds=1))

    assert get_effective_plan(db, user, now=NOW).code == PlanCode.FREE


def test_period_end_is_exclusive_and_start_inclusive(db, user):
    subscription = _subscribe(db, user, start=NOW, end=NOW + timedelta(days=30))

    assert get_effective_plan(db, user, now=NOW).code == PlanCode.PRO
    assert (
        get_effective_plan(db, user, now=subscription.current_period_end).code
        == PlanCode.FREE
    )
    assert (
        get_effective_plan(db, user, now=NOW - timedelta(microseconds=1)).code
        == PlanCode.FREE
    )


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.PENDING,
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.EXPIRED,
    ],
)
def test_statuses_without_paid_access_give_free(db, user, status):
    _subscribe(db, user, status=status)

    assert get_effective_plan(db, user, now=NOW).code == PlanCode.FREE


def test_paid_access_statuses_are_exactly_active_and_cancelled():
    assert PAID_ACCESS_STATUSES == {
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.CANCELLED,
    }


def test_cancelled_subscription_keeps_access_until_period_end(db, user):
    end = NOW + timedelta(days=5)
    _subscribe(db, user, status=SubscriptionStatus.CANCELLED, end=end)

    assert get_effective_plan(db, user, now=NOW).code == PlanCode.PRO
    assert get_effective_plan(db, user, now=end).code == PlanCode.FREE


def test_subscription_without_period_gives_free(db, user):
    _subscribe(db, user, start=None, end=None)

    assert get_effective_plan(db, user, now=NOW).code == PlanCode.FREE


def test_unknown_or_free_plan_code_gives_free(db, user, caplog):
    other = _make_user(db, "bob@example.com")
    _subscribe(db, user, plan_code="enterprise")
    _subscribe(db, other, plan_code="free")

    with caplog.at_level(logging.WARNING):
        assert get_effective_plan(db, user, now=NOW).code == PlanCode.FREE
    assert "subscription_unknown_plan" in caplog.text
    assert get_effective_plan(db, other, now=NOW).code == PlanCode.FREE


def test_subscription_is_per_user(db, user):
    other = _make_user(db, "bob@example.com")
    _subscribe(db, user)

    assert get_effective_plan(db, other, now=NOW).code == PlanCode.FREE


def test_effective_plan_handles_other_time_zones(db, user):
    ist = timezone(timedelta(hours=5, minutes=30))
    end = datetime(2026, 11, 20, 5, 30, tzinfo=ist)  # 00:00 UTC
    _subscribe(db, user, end=end)
    # Reload from the database (SQLite returns naive UTC values).
    db.expire_all()

    just_before = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc)
    assert get_effective_plan(db, user, now=just_before).code == PlanCode.PRO
    at_end = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
    assert get_effective_plan(db, user, now=at_end).code == PlanCode.FREE


def test_naive_timestamps_are_rejected_by_the_models(user):
    with pytest.raises(ValueError):
        Subscription(user_id=user.id, current_period_end=datetime(2026, 12, 1))
    with pytest.raises(ValueError):
        UsageRecord(user_id=user.id, occurred_at=datetime(2026, 12, 1))


def test_naive_now_is_rejected(db, user):
    with pytest.raises(ValueError):
        get_effective_plan(db, user, now=datetime(2026, 11, 17))


def test_effective_plan_uses_setting_overrides(db, user):
    _subscribe(db, user)
    settings = Settings(BILLING_PRO_MONTHLY_EXPORTS=999)

    plan = get_effective_plan(db, user, now=NOW, settings=settings)

    assert plan.limits.monthly_exports == 999


def test_one_subscription_row_per_user(db, user):
    _subscribe(db, user)

    with pytest.raises(IntegrityError):
        _subscribe(db, user)


# --- Usage periods and aggregation ---------------------------------------------


def test_calendar_month_period_is_utc():
    assert calendar_month_period(NOW) == (
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    assert calendar_month_period(
        datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)
    ) == (
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    # 1 Dec 03:00 in India is still 30 Nov in UTC.
    ist = timezone(timedelta(hours=5, minutes=30))
    start, _ = calendar_month_period(datetime(2026, 12, 1, 3, 0, tzinfo=ist))
    assert start == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_free_usage_excludes_previous_months(db, user):
    record_usage(
        db,
        user,
        UsageMetric.UPLOAD_MINUTES,
        30,
        reference_id="video-october",
        occurred_at=datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc),
    )
    record_usage(
        db,
        user,
        "upload_minutes",
        7,
        reference_id="video-1",
        occurred_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    record_usage(
        db,
        user,
        UsageMetric.UPLOAD_MINUTES,
        5,
        reference_id="video-2",
        occurred_at=NOW,
    )
    record_usage(
        db, user, UsageMetric.EXPORTS, 2, reference_id="export-1", occurred_at=NOW
    )
    # Next month and another user's usage do not count either.
    record_usage(
        db,
        user,
        UsageMetric.EXPORTS,
        9,
        reference_id="export-december",
        occurred_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    other = _make_user(db, "bob@example.com")
    record_usage(
        db, other, UsageMetric.EXPORTS, 4, reference_id="export-1", occurred_at=NOW
    )

    summary = get_usage_for_current_period(db, user, now=NOW)

    assert summary.plan.code == PlanCode.FREE
    assert summary.period_start == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert summary.period_end == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert summary.upload_minutes == 12
    assert summary.exports == 2


def test_paid_usage_uses_the_subscription_period(db, user):
    start = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    end = datetime(2026, 12, 10, 8, 0, tzinfo=timezone.utc)
    _subscribe(db, user, start=start, end=end)
    # Same calendar month, but before the paid period started.
    record_usage(
        db,
        user,
        UsageMetric.EXPORTS,
        3,
        reference_id="before",
        occurred_at=start - timedelta(seconds=1),
    )
    record_usage(
        db, user, UsageMetric.EXPORTS, 4, reference_id="at-start", occurred_at=start
    )
    record_usage(
        db, user, UsageMetric.EXPORTS, 6, reference_id="in-period", occurred_at=NOW
    )
    record_usage(
        db, user, UsageMetric.EXPORTS, 8, reference_id="at-end", occurred_at=end
    )
    db.expire_all()

    summary = get_usage_for_current_period(db, user, now=NOW)

    assert summary.plan.code == PlanCode.PRO
    assert summary.period_start == start
    assert summary.period_end == end
    assert summary.exports == 10
    assert summary.upload_minutes == 0


def test_usage_after_subscription_ends_uses_calendar_month(db, user):
    _subscribe(
        db,
        user,
        start=datetime(2026, 10, 5, tzinfo=timezone.utc),
        end=datetime(2026, 11, 5, tzinfo=timezone.utc),
    )

    plan, start, end = get_current_period(db, user, now=NOW)

    assert plan.code == PlanCode.FREE
    assert (start, end) == (
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 12, 1, tzinfo=timezone.utc),
    )


def test_usage_summary_is_zero_without_records(db, user):
    summary = get_usage_for_current_period(db, user, now=NOW)

    assert summary.upload_minutes == 0
    assert summary.exports == 0


# --- Recording usage -------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [
        (0, 0),
        (0.0004, 0),
        (0.5, 1),
        (59.999, 1),
        (60, 1),
        (60.001, 2),
        (61, 2),
        (120.0000000001, 2),
        (7200, 120),
    ],
)
def test_upload_minutes_round_up_to_whole_minutes(seconds, minutes):
    assert upload_minutes_from_seconds(seconds) == minutes


@pytest.mark.parametrize(
    "seconds", [-1, -0.001, float("nan"), float("inf"), True, "60", None]
)
def test_upload_minutes_reject_invalid_durations(seconds):
    with pytest.raises(ValueError):
        upload_minutes_from_seconds(seconds)


def test_record_usage_stores_utc_record(db, user, caplog):
    ist = timezone(timedelta(hours=5, minutes=30))
    with caplog.at_level(logging.INFO):
        record = record_usage(
            db,
            user,
            UsageMetric.UPLOAD_MINUTES,
            3,
            reference_id=" video-1 ",
            occurred_at=datetime(2026, 11, 17, 17, 30, tzinfo=ist),
        )

    assert record.user_id == user.id
    assert record.metric == UsageMetric.UPLOAD_MINUTES
    assert record.quantity == 3
    assert record.reference_id == "video-1"
    assert record.occurred_at == NOW
    assert record.occurred_at.tzinfo == timezone.utc
    assert "usage_recorded" in caplog.text


def test_record_usage_defaults_occurred_at_to_now(db, user):
    before = datetime.now(timezone.utc)
    record = record_usage(db, user, UsageMetric.EXPORTS, 1, reference_id="e")

    assert before <= record.occurred_at <= datetime.now(timezone.utc)


def test_record_usage_is_idempotent_per_reference(db, user):
    first = record_usage(
        db, user, UsageMetric.EXPORTS, 1, reference_id="export-1", occurred_at=NOW
    )
    again = record_usage(
        db, user, UsageMetric.EXPORTS, 1, reference_id="export-1", occurred_at=NOW
    )

    assert again.id == first.id
    assert _usage_count(db) == 1
    assert get_usage_for_current_period(db, user, now=NOW).exports == 1


def test_same_reference_for_another_metric_is_separate(db, user):
    record_usage(db, user, UsageMetric.EXPORTS, 1, reference_id="x", occurred_at=NOW)
    record_usage(
        db, user, UsageMetric.UPLOAD_MINUTES, 2, reference_id="x", occurred_at=NOW
    )

    assert _usage_count(db) == 2


def test_record_usage_rejects_reused_reference_with_other_quantity(db, user):
    record_usage(db, user, UsageMetric.EXPORTS, 1, reference_id="e", occurred_at=NOW)

    with pytest.raises(UsageConflictError):
        record_usage(
            db, user, UsageMetric.EXPORTS, 2, reference_id="e", occurred_at=NOW
        )
    assert _usage_count(db) == 1


def test_record_usage_survives_concurrent_duplicate(db, user, monkeypatch):
    # Simulates a concurrent call: the pre-check misses the existing record,
    # so the unique constraint must reject the insert.
    first = record_usage(
        db, user, UsageMetric.EXPORTS, 1, reference_id="e", occurred_at=NOW
    )
    real_find = billing_service._find_usage
    calls = []

    def racing_find(*args):
        calls.append(args)
        if len(calls) == 1:
            return None
        return real_find(*args)

    monkeypatch.setattr(billing_service, "_find_usage", racing_find)

    again = record_usage(
        db, user, UsageMetric.EXPORTS, 1, reference_id="e", occurred_at=NOW
    )

    assert again.id == first.id
    assert len(calls) == 2
    assert _usage_count(db) == 1


@pytest.mark.parametrize(
    ("metric", "quantity", "reference_id", "occurred_at"),
    [
        (UsageMetric.EXPORTS, -1, "r", NOW),
        (UsageMetric.EXPORTS, 1.5, "r", NOW),
        (UsageMetric.EXPORTS, True, "r", NOW),
        ("seconds", 1, "r", NOW),
        (UsageMetric.EXPORTS, 1, "", NOW),
        (UsageMetric.EXPORTS, 1, "   ", NOW),
        (UsageMetric.EXPORTS, 1, "r" * 256, NOW),
        (UsageMetric.EXPORTS, 1, "bad\nref", NOW),
        (UsageMetric.EXPORTS, 1, "r", datetime(2026, 11, 17)),
    ],
)
def test_record_usage_rejects_invalid_input(
    db, user, metric, quantity, reference_id, occurred_at
):
    with pytest.raises(ValueError):
        record_usage(
            db,
            user,
            metric,
            quantity,
            reference_id=reference_id,
            occurred_at=occurred_at,
        )
    assert _usage_count(db) == 0


def test_database_rejects_negative_usage_quantity(db, user):
    db.add(
        UsageRecord(
            user_id=user.id,
            metric=UsageMetric.EXPORTS,
            quantity=-1,
            occurred_at=NOW,
            reference_id="r",
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


# --- Payments ----------------------------------------------------------------------


def test_record_payment_stores_integer_minor_units(db, user, caplog):
    subscription = _subscribe(db, user)

    with caplog.at_level(logging.INFO):
        payment = record_payment(
            db,
            user,
            amount_minor=99900,
            provider_payment_id="pay_TEST0000000001",
            status=PaymentStatus.CAPTURED,
            subscription=subscription,
        )

    assert payment.amount_minor == 99900
    assert payment.currency == "INR"
    assert payment.status == PaymentStatus.CAPTURED
    assert payment.subscription_id == subscription.id
    assert payment.created_at is not None
    assert "payment_recorded" in caplog.text


def test_duplicate_provider_payment_id_is_rejected(db, user):
    record_payment(
        db,
        user,
        amount_minor=99900,
        provider_payment_id="pay_TEST0000000001",
        status="captured",
    )

    with pytest.raises(DuplicatePaymentError):
        record_payment(
            db,
            user,
            amount_minor=99900,
            provider_payment_id="pay_TEST0000000001",
            status="captured",
        )
    assert db.scalar(select(func.count()).select_from(Payment)) == 1


def test_database_rejects_duplicate_provider_payment_id(db, user):
    for _ in range(2):
        db.add(
            Payment(
                user_id=user.id,
                amount_minor=100,
                currency="INR",
                status=PaymentStatus.CAPTURED,
                provider_payment_id="pay_TEST0000000002",
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()


def test_duplicate_payment_race_is_reported_as_duplicate(db, user):
    # Another process inserts the payment between the pre-check and commit.
    db.execute(
        text(
            "INSERT INTO payments (id, user_id, amount_minor, currency, status, "
            "provider_payment_id) VALUES (:id, :user_id, 100, 'INR', 'captured', "
            "'pay_TEST0000000003')"
        ),
        {"id": uuid.uuid4().hex, "user_id": user.id.hex},
    )
    db.commit()
    original_scalars = db.scalars
    calls = []

    def racing_scalars(statement, *args, **kwargs):
        calls.append(statement)
        result = original_scalars(statement, *args, **kwargs)
        if len(calls) == 1:
            # Hide the existing row from the pre-check only.
            return original_scalars(select(Payment.id).where(text("1 = 0")))
        return result

    db.scalars = racing_scalars  # type: ignore[method-assign]

    with pytest.raises(DuplicatePaymentError) as exc_info:
        record_payment(
            db,
            user,
            amount_minor=100,
            provider_payment_id="pay_TEST0000000003",
            status=PaymentStatus.CAPTURED,
        )
    assert isinstance(exc_info.value.__cause__, IntegrityError)


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_minor": -1},
        {"amount_minor": 999.5},
        {"amount_minor": True},
        {"provider_payment_id": ""},
        {"provider_payment_id": "p" * 256},
        {"currency": "inr"},
        {"currency": "RUPEE"},
        {"status": "paid"},
    ],
)
def test_record_payment_rejects_invalid_input(db, user, overrides):
    values = {
        "amount_minor": 100,
        "provider_payment_id": "pay_TEST0000000004",
        "status": PaymentStatus.CAPTURED,
        **overrides,
    }

    with pytest.raises(ValueError):
        record_payment(db, user, **values)
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


def test_record_payment_rejects_another_users_subscription(db, user):
    other = _make_user(db, "bob@example.com")
    subscription = _subscribe(db, other)

    with pytest.raises(ValueError):
        record_payment(
            db,
            user,
            amount_minor=100,
            provider_payment_id="pay_TEST0000000005",
            status=PaymentStatus.CAPTURED,
            subscription=subscription,
        )


def test_database_rejects_negative_payment_amount(db, user):
    db.add(
        Payment(
            user_id=user.id,
            amount_minor=-1,
            currency="INR",
            status=PaymentStatus.CAPTURED,
            provider_payment_id="pay_TEST0000000006",
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_reprs_do_not_include_provider_ids(db, user):
    subscription = _subscribe(db, user)
    payment = record_payment(
        db,
        user,
        amount_minor=100,
        provider_payment_id="pay_TEST0000000007",
        status=PaymentStatus.CAPTURED,
    )

    assert subscription.provider_subscription_id not in repr(subscription)
    assert "pay_TEST0000000007" not in repr(payment)
