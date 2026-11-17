import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.api.v1.billing import get_payment_provider
from app.billing.plans import PlanCode, get_plan
from app.billing.providers import (
    BillingNotConfiguredError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
    RazorpayProvider,
    build_payment_provider,
)
from app.billing.service import get_effective_plan
from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.billing import Subscription, SubscriptionStatus
from app.models.user import User

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"
SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
# Test-mode placeholders; no real Razorpay account is used.
KEY_ID = "rzp_test_FakeKey0000001"
KEY_SECRET = "test-only-razorpay-key-secret"
WEBHOOK_SECRET = "test-only-razorpay-webhook-secret"
PLAN_ID = "plan_TEST0000000001"
API_BASE = "https://api.razorpay.com/v1"

CHECKOUT_URL = "/api/v1/billing/checkout"
VERIFY_URL = "/api/v1/billing/verify"
CANCEL_URL = "/api/v1/billing/cancel"

RAZORPAY_ENV = (
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "RAZORPAY_PRO_PLAN_ID",
    "RAZORPAY_TIMEOUT_SECONDS",
    "RAZORPAY_SUBSCRIPTION_TOTAL_COUNT",
)


class FakeRazorpay:
    """An in-memory stand-in for the Razorpay subscriptions API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.subscriptions: dict[str, dict] = {}
        self.counter = 0
        # Set to an httpx.Response to return it, or an exception to raise it.
        self.failure: httpx.Response | Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.failure, Exception):
            raise self.failure
        if self.failure is not None:
            return self.failure
        path = request.url.path
        if request.method == "POST" and path == "/v1/subscriptions":
            body = json.loads(request.content)
            self.counter += 1
            subscription_id = f"sub_TEST{self.counter:010d}"
            data = {
                "id": subscription_id,
                "entity": "subscription",
                "plan_id": body["plan_id"],
                "status": "created",
                "current_start": None,
                "current_end": None,
            }
            self.subscriptions[subscription_id] = data
            return httpx.Response(200, json=data)
        match = re.fullmatch(r"/v1/subscriptions/(\w+)(/cancel)?", path)
        if match and match.group(1) in self.subscriptions:
            data = self.subscriptions[match.group(1)]
            if match.group(2) and request.method == "POST":
                if data["status"] in ("cancelled", "completed", "expired"):
                    return httpx.Response(
                        400, json={"error": {"code": "BAD_REQUEST_ERROR"}}
                    )
                # cancel_at_cycle_end 1: Razorpay keeps it active until the
                # end; 0 cancels at once.
                if json.loads(request.content).get("cancel_at_cycle_end") == 0:
                    data["status"] = "cancelled"
                return httpx.Response(200, json=data)
            if request.method == "GET" and not match.group(2):
                return httpx.Response(200, json=data)
        return httpx.Response(404, json={"error": {"code": "BAD_REQUEST_ERROR"}})

    def activate(self, subscription_id: str, start: int, end: int) -> None:
        self.subscriptions[subscription_id].update(
            status="active", current_start=start, current_end=end
        )


@pytest.fixture
def session_factory():
    engine = create_db_engine(
        SQLITE_IN_MEMORY_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def fake_razorpay():
    return FakeRazorpay()


@pytest.fixture
def make_client(session_factory, monkeypatch, fake_razorpay):
    """Build a TestClient whose Razorpay calls go to `fake_razorpay`."""
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    for name in RAZORPAY_ENV:
        monkeypatch.delenv(name, raising=False)

    def factory(*, configured: bool = True, plan_id: bool = True) -> TestClient:
        if configured:
            monkeypatch.setenv("RAZORPAY_KEY_ID", KEY_ID)
            monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
            monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
        if plan_id:
            monkeypatch.setenv("RAZORPAY_PRO_PLAN_ID", PLAN_ID)
        get_settings.cache_clear()
        application = create_app()

        def override_get_db():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_db] = override_get_db
        if configured:
            # The real dependency with a mocked transport: no network access.
            application.dependency_overrides[get_payment_provider] = (
                lambda: RazorpayProvider.from_settings(
                    get_settings(), transport=httpx.MockTransport(fake_razorpay)
                )
            )
        return TestClient(application)

    return factory


@pytest.fixture
def client(make_client):
    return make_client()


def _make_user(db, email="alice@example.com") -> User:
    user = User(email=email, hashed_password="x")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def user(db):
    return _make_user(db)


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user.id, token_version=user.token_version, settings=get_settings()
    )
    return {"Authorization": f"Bearer {token}"}


def _sign(payment_id: str, subscription_id: str, secret: str = KEY_SECRET) -> str:
    message = f"{payment_id}|{subscription_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _basic_auth() -> str:
    return "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()


def _stored(db, user) -> Subscription | None:
    db.expire_all()
    return db.scalars(
        select(Subscription).where(Subscription.user_id == user.id)
    ).one_or_none()


def _subscription_count(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Subscription))


def _checkout(client, user) -> str:
    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))
    assert response.status_code == 200, response.text
    return response.json()["subscription_id"]


def _period() -> tuple[int, int]:
    now = int(time.time())
    return now - 60, now + 30 * 24 * 3600


def _verify_body(subscription_id, payment_id="pay_TEST0000000001", signature=None):
    return {
        "razorpay_payment_id": payment_id,
        "razorpay_subscription_id": subscription_id,
        "razorpay_signature": signature or _sign(payment_id, subscription_id),
    }


def _activate(client, user, fake_razorpay) -> str:
    subscription_id = _checkout(client, user)
    fake_razorpay.activate(subscription_id, *_period())
    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )
    assert response.status_code == 200, response.text
    return subscription_id


# --- Authentication and configuration ---------------------------------------


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (CHECKOUT_URL, {"plan_code": "pro"}),
        (VERIFY_URL, _verify_body("sub_TEST0000000001")),
        (CANCEL_URL, None),
    ],
)
def test_billing_endpoints_require_authentication(client, fake_razorpay, url, body):
    response = client.post(url, json=body)

    assert response.status_code == 401
    assert fake_razorpay.requests == []


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (CHECKOUT_URL, {"plan_code": "pro"}),
        (VERIFY_URL, _verify_body("sub_TEST0000000001")),
        (CANCEL_URL, None),
    ],
)
def test_unconfigured_billing_returns_503(make_client, user, db, url, body):
    client = make_client(configured=False)

    response = client.post(url, json=body, headers=_auth(user))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "billing_not_configured"
    assert _subscription_count(db) == 0


def test_invalid_key_id_is_treated_as_not_configured(make_client, user, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "<your-razorpay-key-id>")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
    client = make_client(configured=False)

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "billing_not_configured"


def test_checkout_without_plan_id_returns_503(make_client, user, fake_razorpay, db):
    client = make_client(plan_id=False)

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "billing_not_configured"
    assert fake_razorpay.requests == []
    assert _subscription_count(db) == 0


# --- Checkout ----------------------------------------------------------------


def test_checkout_sends_server_side_plan_with_basic_auth(
    client, user, fake_razorpay, db
):
    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 200
    assert response.json() == {
        "provider": "razorpay",
        "key_id": KEY_ID,
        "subscription_id": "sub_TEST0000000001",
    }
    assert KEY_SECRET not in response.text
    assert len(fake_razorpay.requests) == 1
    request = fake_razorpay.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{API_BASE}/subscriptions"
    assert request.headers["authorization"] == _basic_auth()
    assert json.loads(request.content) == {
        "plan_id": PLAN_ID,
        "total_count": 60,
        "quantity": 1,
        "customer_notify": 1,
        "notes": {"user_id": str(user.id), "plan_code": "pro"},
    }


def test_checkout_creates_pending_subscription_without_access(
    client, user, fake_razorpay, db
):
    subscription_id = _checkout(client, user)

    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.plan_code == "pro"
    assert subscription.provider == "razorpay"
    assert subscription.provider_subscription_id == subscription_id
    assert subscription.current_period_start is None
    assert subscription.current_period_end is None
    assert get_effective_plan(db, user).code == PlanCode.FREE


def test_checkout_is_idempotent_while_pending(client, user, fake_razorpay, db):
    first = _checkout(client, user)
    second = _checkout(client, user)

    assert first == second
    # One subscription created; the second checkout only confirmed it.
    assert [(r.method, r.url.path) for r in fake_razorpay.requests] == [
        ("POST", "/v1/subscriptions"),
        ("GET", f"/v1/subscriptions/{first}"),
    ]
    assert _subscription_count(db) == 1


@pytest.mark.parametrize("provider_status", ["expired", "cancelled", "halted"])
def test_checkout_replaces_a_pending_checkout_that_ended_at_the_provider(
    client, user, fake_razorpay, db, provider_status
):
    first = _checkout(client, user)
    fake_razorpay.subscriptions[first]["status"] = provider_status

    second = _checkout(client, user)

    assert second != first
    assert fake_razorpay.subscriptions[second]["status"] == "created"
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.provider_subscription_id == second
    assert _subscription_count(db) == 1
    # The new checkout can be completed.
    fake_razorpay.activate(second, *_period())
    response = client.post(VERIFY_URL, json=_verify_body(second), headers=_auth(user))
    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_checkout_after_verify_rejected_terminal_state_starts_a_new_one(
    client, user, fake_razorpay, db
):
    first = _checkout(client, user)
    fake_razorpay.activate(first, *_period())
    fake_razorpay.subscriptions[first]["status"] = "cancelled"
    response = client.post(VERIFY_URL, json=_verify_body(first), headers=_auth(user))
    assert response.status_code == 409

    second = _checkout(client, user)

    assert second != first
    assert _stored(db, user).provider_subscription_id == second


@pytest.mark.parametrize("provider_status", ["created", "authenticated", "pending"])
def test_checkout_replaces_a_pending_checkout_for_a_changed_plan_id(
    client, user, fake_razorpay, db, provider_status
):
    first = _checkout(client, user)
    fake_razorpay.subscriptions[first].update(
        plan_id="plan_OLDPLAN000001", status=provider_status
    )

    second = _checkout(client, user)

    assert second != first
    assert fake_razorpay.subscriptions[second]["plan_id"] == PLAN_ID
    assert _stored(db, user).provider_subscription_id == second
    # The abandoned checkout could still charge, so it was cancelled at once
    # before the new one was created.
    cancel_request = fake_razorpay.requests[-2]
    assert cancel_request.method == "POST"
    assert cancel_request.url.path == f"/v1/subscriptions/{first}/cancel"
    assert json.loads(cancel_request.content) == {"cancel_at_cycle_end": 0}
    assert fake_razorpay.subscriptions[first]["status"] == "cancelled"


def test_checkout_keeps_the_old_checkout_when_cancelling_it_fails(
    client, user, fake_razorpay, db
):
    first = _checkout(client, user)
    fake_razorpay.subscriptions[first].update(
        plan_id="plan_OLDPLAN000001", status="authenticated"
    )

    def failing_cancel(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            fake_razorpay.requests.append(request)
            return httpx.Response(500, json={"error": {}})
        return fake_razorpay(request)

    client.app.dependency_overrides[get_payment_provider] = (
        lambda: RazorpayProvider.from_settings(
            get_settings(), transport=httpx.MockTransport(failing_cancel)
        )
    )

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 502
    # No second Razorpay subscription was created.
    assert len(fake_razorpay.subscriptions) == 1
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.provider_subscription_id == first


def test_checkout_activates_a_paid_checkout_whose_verify_was_lost(
    client, user, fake_razorpay, db
):
    first = _checkout(client, user)
    start, end = _period()
    fake_razorpay.activate(first, start, end)

    # The browser never called /verify; the user starts a checkout again.
    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subscription_exists"
    # Activated from Razorpay's confirmed state; no second subscription.
    assert len(fake_razorpay.subscriptions) == 1
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.provider_subscription_id == first
    assert subscription.current_period_end.replace(
        tzinfo=timezone.utc
    ) == datetime.fromtimestamp(end, timezone.utc)
    assert get_effective_plan(db, user).code == PlanCode.PRO
    # A late verify callback is a harmless duplicate, and it can be cancelled.
    response = client.post(VERIFY_URL, json=_verify_body(first), headers=_auth(user))
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert client.post(CANCEL_URL, headers=_auth(user)).status_code == 200


@pytest.mark.parametrize(
    "remote",
    [
        {"status": "paused"},
        # Active, but for another plan id or without a valid period.
        {"status": "active", "plan_id": "plan_OLDPLAN000001"},
        {"status": "active", "current_start": None, "current_end": None},
    ],
)
def test_checkout_does_not_activate_or_replace_an_unconfirmed_paid_checkout(
    client, user, fake_razorpay, db, remote
):
    first = _checkout(client, user)
    fake_razorpay.activate(first, *_period())
    fake_razorpay.subscriptions[first].update(remote)

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "checkout_unsettled"
    assert len(fake_razorpay.subscriptions) == 1
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.provider_subscription_id == first
    assert get_effective_plan(db, user).code == PlanCode.FREE


def test_checkout_reuse_provider_timeout_keeps_the_pending_checkout(
    client, user, fake_razorpay, db
):
    first = _checkout(client, user)
    fake_razorpay.failure = httpx.ReadTimeout("timed out")

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 504
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.provider_subscription_id == first


def test_checkout_for_free_plan_is_rejected(client, user, fake_razorpay, db):
    response = client.post(
        CHECKOUT_URL, json={"plan_code": "free"}, headers=_auth(user)
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "plan_not_purchasable"
    assert fake_razorpay.requests == []
    assert _subscription_count(db) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"plan_code": "enterprise"},
        {"plan_code": "pro", "amount": 1},
        {"plan_code": "pro", "price_minor": 100},
        {},
    ],
)
def test_checkout_rejects_unknown_plans_and_client_prices(
    client, user, fake_razorpay, body
):
    response = client.post(CHECKOUT_URL, json=body, headers=_auth(user))

    assert response.status_code == 422
    assert fake_razorpay.requests == []


def test_checkout_rejected_while_subscribed(client, user, fake_razorpay, db):
    now = datetime.now(timezone.utc)
    db.add(
        Subscription(
            user_id=user.id,
            plan_code="pro",
            status=SubscriptionStatus.ACTIVE,
            provider="manual",
            current_period_start=now - timedelta(days=1),
            current_period_end=now + timedelta(days=29),
        )
    )
    db.commit()

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subscription_exists"
    assert fake_razorpay.requests == []
    assert _stored(db, user).status == SubscriptionStatus.ACTIVE


def test_checkout_after_expired_subscription_starts_a_new_one(
    client, user, fake_razorpay, db
):
    now = datetime.now(timezone.utc)
    db.add(
        Subscription(
            user_id=user.id,
            plan_code="pro",
            status=SubscriptionStatus.CANCELLED,
            provider="razorpay",
            provider_subscription_id="sub_OLD0000000001",
            current_period_start=now - timedelta(days=60),
            current_period_end=now - timedelta(days=30),
        )
    )
    db.commit()
    fake_razorpay.subscriptions["sub_OLD0000000001"] = {
        "id": "sub_OLD0000000001",
        "entity": "subscription",
        "plan_id": PLAN_ID,
        "status": "cancelled",
        "current_start": None,
        "current_end": None,
    }

    subscription_id = _checkout(client, user)

    # The old subscription was confirmed ended at Razorpay before replacing it.
    first_request = fake_razorpay.requests[0]
    assert first_request.method == "GET"
    assert first_request.url.path == "/v1/subscriptions/sub_OLD0000000001"
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.provider_subscription_id == subscription_id
    assert subscription.current_period_end is None


def _lapse_local_period(db, user) -> None:
    """Move the stored period into the past, as when renewals are not synced."""
    subscription = _stored(db, user)
    now = datetime.now(timezone.utc)
    subscription.current_period_start = now - timedelta(days=60)
    subscription.current_period_end = now - timedelta(days=30)
    db.commit()


@pytest.mark.parametrize("provider_status", ["active", "pending", "authenticated"])
def test_checkout_does_not_replace_a_live_subscription_with_a_lapsed_period(
    client, user, fake_razorpay, db, provider_status
):
    first = _activate(client, user, fake_razorpay)
    _lapse_local_period(db, user)
    fake_razorpay.subscriptions[first]["status"] = provider_status
    assert get_effective_plan(db, user).code == PlanCode.FREE

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subscription_exists"
    last_request = fake_razorpay.requests[-1]
    assert last_request.method == "GET"
    assert last_request.url.path == f"/v1/subscriptions/{first}"
    # No second Razorpay subscription; the original stays referenced.
    assert len(fake_razorpay.subscriptions) == 1
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.provider_subscription_id == first
    # It can still be cancelled through ClipForge.
    fake_razorpay.subscriptions[first]["status"] = "active"
    assert client.post(CANCEL_URL, headers=_auth(user)).status_code == 200


@pytest.mark.parametrize(
    "provider_status", ["cancelled", "completed", "expired", "halted"]
)
def test_checkout_replaces_a_lapsed_subscription_that_ended_at_the_provider(
    client, user, fake_razorpay, db, provider_status
):
    first = _activate(client, user, fake_razorpay)
    _lapse_local_period(db, user)
    fake_razorpay.subscriptions[first]["status"] = provider_status

    second = _checkout(client, user)

    assert second != first
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.provider_subscription_id == second
    assert subscription.current_period_end is None


def test_checkout_lapsed_subscription_provider_timeout_changes_nothing(
    client, user, fake_razorpay, db
):
    first = _activate(client, user, fake_razorpay)
    _lapse_local_period(db, user)
    fake_razorpay.failure = httpx.ReadTimeout("timed out")

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 504
    assert len(fake_razorpay.subscriptions) == 1
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.provider_subscription_id == first


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"error": {"description": KEY_SECRET}}),
        httpx.Response(401, json={"error": {"code": "BAD_REQUEST_ERROR"}}),
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"id": "../../payments", "status": "created"}),
        httpx.Response(
            200, json={"id": "sub_X", "status": "created", "plan_id": "plan_OTHER"}
        ),
        httpx.Response(200, json={"id": "sub_X", "status": 7}),
    ],
)
def test_checkout_provider_failure_returns_502_without_a_checkout(
    client, user, fake_razorpay, db, caplog, failure
):
    fake_razorpay.failure = failure
    caplog.set_level(logging.DEBUG)

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "payment_provider_error"
    assert KEY_SECRET not in response.text
    for record in caplog.records:
        assert KEY_SECRET not in str(record.__dict__)
    subscription = _stored(db, user)
    assert subscription is None or subscription.provider_subscription_id is None
    assert get_effective_plan(db, user).code == PlanCode.FREE

    # A later attempt succeeds once the provider recovers.
    fake_razorpay.failure = None
    assert _checkout(client, user) == "sub_TEST0000000001"


@pytest.mark.parametrize(
    ("exception", "status_code", "code"),
    [
        (httpx.ReadTimeout("timed out"), 504, "payment_provider_timeout"),
        (httpx.ConnectTimeout("timed out"), 504, "payment_provider_timeout"),
        (httpx.ConnectError("refused"), 502, "payment_provider_error"),
    ],
)
def test_checkout_timeouts_and_network_errors(
    client, user, fake_razorpay, db, exception, status_code, code
):
    fake_razorpay.failure = exception

    response = client.post(CHECKOUT_URL, json={"plan_code": "pro"}, headers=_auth(user))

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    subscription = _stored(db, user)
    assert subscription is None or subscription.provider_subscription_id is None


# --- Verify ------------------------------------------------------------------


def test_valid_signature_and_active_provider_state_activate(
    client, user, fake_razorpay, db
):
    subscription_id = _checkout(client, user)
    start, end = _period()
    fake_razorpay.activate(subscription_id, start, end)

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["plan_code"] == "pro"
    assert datetime.fromisoformat(body["current_period_end"]) == datetime.fromtimestamp(
        end, timezone.utc
    )
    # The provider-side state was confirmed before activation.
    request = fake_razorpay.requests[-1]
    assert request.method == "GET"
    assert str(request.url) == f"{API_BASE}/subscriptions/{subscription_id}"
    assert request.headers["authorization"] == _basic_auth()

    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.current_period_start.replace(
        tzinfo=timezone.utc
    ) == datetime.fromtimestamp(start, timezone.utc)
    assert get_effective_plan(db, user).code == PlanCode.PRO


def test_duplicate_verify_callback_is_idempotent(client, user, fake_razorpay, db):
    subscription_id = _activate(client, user, fake_razorpay)
    request_count = len(fake_razorpay.requests)

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert len(fake_razorpay.requests) == request_count
    assert _stored(db, user).status == SubscriptionStatus.ACTIVE


@pytest.mark.parametrize(
    "make_signature",
    [
        lambda pay, sub: "0" * 64,
        lambda pay, sub: _sign(pay, sub, secret=WEBHOOK_SECRET),
        lambda pay, sub: _sign(sub, pay),
        lambda pay, sub: _sign(pay, sub).upper(),
        lambda pay, sub: "é" * 10,
    ],
)
def test_invalid_signature_returns_400_without_state_change(
    client, user, fake_razorpay, db, make_signature
):
    subscription_id = _checkout(client, user)
    fake_razorpay.activate(subscription_id, *_period())
    payment_id = "pay_TEST0000000001"

    response = client.post(
        VERIFY_URL,
        json=_verify_body(
            subscription_id, payment_id, make_signature(payment_id, subscription_id)
        ),
        headers=_auth(user),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    # Only the checkout call; the provider was never asked to confirm.
    assert len(fake_razorpay.requests) == 1
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.current_period_end is None
    assert get_effective_plan(db, user).code == PlanCode.FREE


@pytest.mark.parametrize("provider_status", ["created", "authenticated", "pending"])
def test_verify_keeps_pending_until_provider_reports_active(
    client, user, fake_razorpay, db, provider_status
):
    subscription_id = _checkout(client, user)
    fake_razorpay.subscriptions[subscription_id]["status"] = provider_status

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert _stored(db, user).status == SubscriptionStatus.PENDING
    assert get_effective_plan(db, user).code == PlanCode.FREE


@pytest.mark.parametrize("provider_status", ["halted", "cancelled", "expired"])
def test_verify_rejects_inactive_provider_state(
    client, user, fake_razorpay, db, provider_status
):
    subscription_id = _checkout(client, user)
    fake_razorpay.activate(subscription_id, *_period())
    fake_razorpay.subscriptions[subscription_id]["status"] = provider_status

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subscription_not_active"
    assert _stored(db, user).status == SubscriptionStatus.PENDING


def test_verify_rejects_a_different_plan_at_the_provider(
    client, user, fake_razorpay, db
):
    subscription_id = _checkout(client, user)
    fake_razorpay.activate(subscription_id, *_period())
    fake_razorpay.subscriptions[subscription_id]["plan_id"] = "plan_CHEAPER00001"

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subscription_plan_mismatch"
    assert _stored(db, user).status == SubscriptionStatus.PENDING


def test_verify_without_plan_id_returns_503_without_activation(
    make_client, user, fake_razorpay, db, monkeypatch
):
    subscription_id = _checkout(make_client(), user)
    fake_razorpay.activate(subscription_id, *_period())
    fake_razorpay.subscriptions[subscription_id]["plan_id"] = None
    monkeypatch.delenv("RAZORPAY_PRO_PLAN_ID")
    client = make_client(plan_id=False)
    request_count = len(fake_razorpay.requests)

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "billing_not_configured"
    assert len(fake_razorpay.requests) == request_count
    assert _stored(db, user).status == SubscriptionStatus.PENDING
    assert get_effective_plan(db, user).code == PlanCode.FREE


@pytest.mark.parametrize(
    "period",
    [
        {"current_start": None, "current_end": None},
        {"current_start": 1_800_000_000, "current_end": 1_700_000_000},
        {"current_start": "yesterday", "current_end": 1_800_000_000},
    ],
)
def test_verify_rejects_active_state_without_a_valid_period(
    client, user, fake_razorpay, db, period
):
    subscription_id = _checkout(client, user)
    fake_razorpay.subscriptions[subscription_id].update(status="active", **period)

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 502
    assert _stored(db, user).status == SubscriptionStatus.PENDING


def test_verify_provider_timeout_leaves_subscription_pending(
    client, user, fake_razorpay, db
):
    subscription_id = _checkout(client, user)
    fake_razorpay.failure = httpx.ReadTimeout("timed out")

    response = client.post(
        VERIFY_URL, json=_verify_body(subscription_id), headers=_auth(user)
    )

    assert response.status_code == 504
    assert _stored(db, user).status == SubscriptionStatus.PENDING


def test_verify_another_users_subscription_returns_404(client, user, fake_razorpay, db):
    bob = _make_user(db, "bob@example.com")
    alice_subscription = _checkout(client, user)
    _checkout(client, bob)
    fake_razorpay.activate(alice_subscription, *_period())

    response = client.post(
        VERIFY_URL, json=_verify_body(alice_subscription), headers=_auth(bob)
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "subscription_not_found"
    assert _stored(db, user).status == SubscriptionStatus.PENDING
    assert _stored(db, bob).status == SubscriptionStatus.PENDING


def test_verify_without_checkout_returns_404(client, user, fake_razorpay):
    response = client.post(
        VERIFY_URL, json=_verify_body("sub_TEST0000000001"), headers=_auth(user)
    )

    assert response.status_code == 404
    assert fake_razorpay.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {
            "razorpay_payment_id": "pay_1",
            "razorpay_subscription_id": "sub/../x",
            "razorpay_signature": "ab",
        },
        {
            "razorpay_payment_id": "pay_1",
            "razorpay_subscription_id": "sub_1",
            "razorpay_signature": "",
        },
        {"razorpay_payment_id": "pay_1", "razorpay_subscription_id": "sub_1"},
    ],
)
def test_verify_validates_input(client, user, body):
    response = client.post(VERIFY_URL, json=body, headers=_auth(user))

    assert response.status_code == 422


def test_verify_never_logs_secrets_or_signatures(client, user, fake_razorpay, caplog):
    caplog.set_level(logging.DEBUG)
    subscription_id = _checkout(client, user)
    fake_razorpay.activate(subscription_id, *_period())
    body = _verify_body(subscription_id)

    client.post(VERIFY_URL, json=body, headers=_auth(user))
    client.post(
        VERIFY_URL,
        json=_verify_body(subscription_id, signature="1" * 64),
        headers=_auth(user),
    )

    assert any(
        r.getMessage() == "billing_subscription_activated" for r in caplog.records
    )
    for record in caplog.records:
        text = str(record.__dict__)
        assert KEY_SECRET not in text
        assert body["razorpay_signature"] not in text


# --- Cancel ------------------------------------------------------------------


def test_cancel_calls_api_and_keeps_access_until_period_end(
    client, user, fake_razorpay, db
):
    subscription_id = _activate(client, user, fake_razorpay)
    period_end = _stored(db, user).current_period_end

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    request = fake_razorpay.requests[-1]
    assert request.method == "POST"
    assert str(request.url) == f"{API_BASE}/subscriptions/{subscription_id}/cancel"
    assert request.headers["authorization"] == _basic_auth()
    assert json.loads(request.content) == {"cancel_at_cycle_end": 1}

    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.CANCELLED
    assert subscription.current_period_end == period_end
    assert get_effective_plan(db, user).code == PlanCode.PRO


def test_cancel_twice_is_idempotent(client, user, fake_razorpay, db):
    _activate(client, user, fake_razorpay)
    assert client.post(CANCEL_URL, headers=_auth(user)).status_code == 200
    request_count = len(fake_razorpay.requests)

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert len(fake_razorpay.requests) == request_count


def test_cancel_provider_failure_keeps_subscription_active(
    client, user, fake_razorpay, db
):
    _activate(client, user, fake_razorpay)
    fake_razorpay.failure = httpx.Response(500, json={"error": {}})

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 502
    assert _stored(db, user).status == SubscriptionStatus.ACTIVE


def test_cancel_of_subscription_already_cancelled_at_provider(
    client, user, fake_razorpay, db
):
    subscription_id = _activate(client, user, fake_razorpay)
    fake_razorpay.subscriptions[subscription_id]["status"] = "cancelled"

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert [(r.method, r.url.path) for r in fake_razorpay.requests[-2:]] == [
        ("POST", f"/v1/subscriptions/{subscription_id}/cancel"),
        ("GET", f"/v1/subscriptions/{subscription_id}"),
    ]
    assert _stored(db, user).status == SubscriptionStatus.CANCELLED


def test_cancel_rejected_by_provider_for_another_reason_keeps_active(
    client, user, fake_razorpay, db
):
    _activate(client, user, fake_razorpay)
    fake_razorpay.failure = httpx.Response(400, json={"error": {}})

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 502
    assert _stored(db, user).status == SubscriptionStatus.ACTIVE


def test_cancel_without_subscription_returns_404(client, user, fake_razorpay):
    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 404
    assert fake_razorpay.requests == []


def test_cancel_pending_subscription_returns_409(client, user, fake_razorpay, db):
    subscription_id = _checkout(client, user)

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_active_subscription"
    # Only the provider state was read; nothing was cancelled.
    assert [(r.method, r.url.path) for r in fake_razorpay.requests[1:]] == [
        ("GET", f"/v1/subscriptions/{subscription_id}"),
    ]
    assert _stored(db, user).status == SubscriptionStatus.PENDING


def test_cancel_of_a_paid_checkout_whose_verify_was_lost(
    client, user, fake_razorpay, db
):
    subscription_id = _checkout(client, user)
    start, end = _period()
    fake_razorpay.activate(subscription_id, start, end)

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert [(r.method, r.url.path) for r in fake_razorpay.requests[1:]] == [
        ("GET", f"/v1/subscriptions/{subscription_id}"),
        ("POST", f"/v1/subscriptions/{subscription_id}/cancel"),
    ]
    assert json.loads(fake_razorpay.requests[-1].content) == {"cancel_at_cycle_end": 1}
    subscription = _stored(db, user)
    assert subscription.status == SubscriptionStatus.CANCELLED
    assert subscription.current_period_end.replace(
        tzinfo=timezone.utc
    ) == datetime.fromtimestamp(end, timezone.utc)
    # The paid period is kept until its end.
    assert get_effective_plan(db, user).code == PlanCode.PRO


def test_cancel_of_a_paid_checkout_keeps_it_pending_when_the_cancel_fails(
    client, user, fake_razorpay, db
):
    subscription_id = _checkout(client, user)
    fake_razorpay.activate(subscription_id, *_period())

    def failing_cancel(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            fake_razorpay.requests.append(request)
            return httpx.Response(500, json={"error": {}})
        return fake_razorpay(request)

    client.app.dependency_overrides[get_payment_provider] = (
        lambda: RazorpayProvider.from_settings(
            get_settings(), transport=httpx.MockTransport(failing_cancel)
        )
    )

    response = client.post(CANCEL_URL, headers=_auth(user))

    assert response.status_code == 502
    # Nothing was saved; a later checkout or cancel syncs it again.
    assert _stored(db, user).status == SubscriptionStatus.PENDING


def test_cancel_only_affects_the_current_user(client, user, fake_razorpay, db):
    _activate(client, user, fake_razorpay)
    bob = _make_user(db, "bob@example.com")

    response = client.post(CANCEL_URL, headers=_auth(bob))

    assert response.status_code == 404
    assert _stored(db, user).status == SubscriptionStatus.ACTIVE


# --- Provider ----------------------------------------------------------------


def _provider(transport=None, **overrides) -> RazorpayProvider:
    options = {
        "key_id": KEY_ID,
        "key_secret": KEY_SECRET,
        "webhook_secret": WEBHOOK_SECRET,
        "transport": transport,
    }
    options.update(overrides)
    return RazorpayProvider(**options)


def test_payment_signature_verification():
    provider = _provider()
    signature = _sign("pay_1", "sub_1")

    assert provider.verify_payment_signature(
        payment_id="pay_1", subscription_id="sub_1", signature=signature
    )
    assert not provider.verify_payment_signature(
        payment_id="pay_2", subscription_id="sub_1", signature=signature
    )
    assert not provider.verify_payment_signature(
        payment_id="pay_1", subscription_id="sub_1", signature=None
    )
    assert not provider.verify_payment_signature(
        payment_id="pay_1|x", subscription_id="sub_1", signature=signature
    )
    assert not provider.verify_payment_signature(
        payment_id="pay_1", subscription_id="sub_1", signature=signature + "0" * 100
    )


def test_webhook_signature_uses_the_webhook_secret():
    provider = _provider()
    body = b'{"event":"subscription.activated"}'
    valid = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    with_key_secret = hmac.new(KEY_SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert provider.verify_webhook_signature(body, valid)
    assert not provider.verify_webhook_signature(body, with_key_secret)
    assert not provider.verify_webhook_signature(body + b" ", valid)
    assert not provider.verify_webhook_signature(body, "")


def test_webhook_signature_is_rejected_without_a_webhook_secret():
    provider = _provider(webhook_secret=None)
    body = b"{}"
    signature = hmac.new(KEY_SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert not provider.verify_webhook_signature(body, signature)


def test_from_settings_names_missing_settings_without_values(monkeypatch):
    for name in RAZORPAY_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BillingNotConfiguredError) as missing:
        RazorpayProvider.from_settings(Settings())
    assert "RAZORPAY_KEY_ID" in str(missing.value)
    assert "RAZORPAY_KEY_SECRET" in str(missing.value)

    monkeypatch.setenv("RAZORPAY_KEY_ID", "  ")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", KEY_SECRET)
    with pytest.raises(BillingNotConfiguredError) as blank:
        build_payment_provider(Settings())
    assert KEY_SECRET not in str(blank.value)

    monkeypatch.setenv("RAZORPAY_KEY_ID", KEY_ID)
    provider = build_payment_provider(Settings())
    assert isinstance(provider, RazorpayProvider)
    assert provider.key_id == KEY_ID


def test_provider_rejects_unsafe_ids_before_calling_the_api(fake_razorpay):
    provider = _provider(httpx.MockTransport(fake_razorpay))

    for bad_id in ("../payments", "sub_1/cancel", "", "sub 1"):
        with pytest.raises(ValueError):
            provider.fetch_subscription(bad_id)
        with pytest.raises(ValueError):
            provider.cancel_subscription(bad_id)
    assert fake_razorpay.requests == []


def test_provider_errors_and_timeouts(fake_razorpay, monkeypatch):
    monkeypatch.setenv("RAZORPAY_PRO_PLAN_ID", PLAN_ID)
    provider = _provider(httpx.MockTransport(fake_razorpay))
    user = User(email="alice@example.com", hashed_password="x")
    plan = get_plan(PlanCode.PRO, Settings())

    fake_razorpay.failure = httpx.ReadTimeout("timed out")
    with pytest.raises(PaymentProviderTimeoutError):
        provider.create_subscription(user, plan)

    fake_razorpay.failure = httpx.Response(503, json={"error": {}})
    with pytest.raises(PaymentProviderError) as http_error:
        provider.create_subscription(user, plan)
    assert http_error.value.status_code == 503

    with pytest.raises(ValueError):
        provider.create_subscription(user, get_plan(PlanCode.FREE, Settings()))


def test_provider_stops_reading_an_oversized_response():
    chunks_sent = []

    def endless_body():
        for _ in range(100):
            chunks_sent.append(1)
            yield b" " * 65536

    provider = _provider(
        httpx.MockTransport(lambda request: httpx.Response(200, content=endless_body()))
    )

    with pytest.raises(PaymentProviderError):
        provider.fetch_subscription("sub_TEST0000000001")
    # Reading stopped just past the 1 MiB limit instead of buffering 6.25 MiB.
    assert len(chunks_sent) < 20


def test_razorpay_settings_blank_values_are_unset(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", " ")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "  ")

    settings = Settings()

    assert settings.RAZORPAY_KEY_ID is None
    assert settings.RAZORPAY_KEY_SECRET is None
    assert settings.RAZORPAY_WEBHOOK_SECRET is None
    assert settings.RAZORPAY_TIMEOUT_SECONDS == 15.0
    assert settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT == 60
