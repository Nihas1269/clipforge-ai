"""Razorpay subscriptions over the REST API (httpx, no SDK).

Requests go to `https://api.razorpay.com/v1` with HTTP basic auth
(`RAZORPAY_KEY_ID`:`RAZORPAY_KEY_SECRET`), a timeout of
`RAZORPAY_TIMEOUT_SECONDS` (at most 10 s to connect) and no redirects. They
are not retried here.

- create: `POST /subscriptions` with the configured plan id,
  `total_count`, `quantity` 1 and notes (`user_id`, `plan_code`). The price
  is the plan's price in the Razorpay dashboard; no amount is ever sent.
- fetch: `GET /subscriptions/{id}`.
- cancel: `POST /subscriptions/{id}/cancel` with `cancel_at_cycle_end` 1
  (0 to cancel an abandoned checkout at once).

Signatures are hex HMAC-SHA256 compared with `hmac.compare_digest`: the
checkout payment signature over `"{payment_id}|{subscription_id}"` with the
key secret, and the webhook signature over the raw body with the separate
`RAZORPAY_WEBHOOK_SECRET`.

Timeouts, network errors, non-2xx answers and malformed or oversized bodies
raise PaymentProviderError (PaymentProviderTimeoutError for timeouts) and are
logged as `razorpay_request_failed` with the operation, status code and
reason. Secrets, payloads, response bodies and provider ids are never logged
or put into error messages.
"""

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, TypeGuard

import httpx

from app.billing.plans import Plan
from app.billing.providers.base import (
    BillingNotConfiguredError,
    CheckoutSession,
    PaymentProviderError,
    PaymentProviderTimeoutError,
    ProviderSubscription,
)
from app.core.config import Settings
from app.models.user import User

logger = logging.getLogger(__name__)

PROVIDER_NAME = "razorpay"
RAZORPAY_API_BASE_URL = "https://api.razorpay.com/v1"
CONNECT_TIMEOUT_SECONDS = 10.0
# Razorpay answers are a few KiB; anything larger is refused.
MAX_RESPONSE_BYTES = 1024 * 1024

# Key ids look like "rzp_test_..." or "rzp_live_...".
KEY_ID_PATTERN = re.compile(r"rzp_(?:test|live)_[A-Za-z0-9]{1,64}")
# Ids such as "sub_...", "pay_..." and "plan_..."; also keeps ids safe to use
# in a URL path.
PROVIDER_ID_PATTERN = re.compile(r"[A-Za-z0-9_]{1,64}")
STATUS_PATTERN = re.compile(r"[a-z_]{1,32}")
SIGNATURE_MAX_LENGTH = 128
# Unix timestamps accepted from the provider (up to the year 2286).
MAX_TIMESTAMP = 10_000_000_000


def is_provider_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and PROVIDER_ID_PATTERN.fullmatch(value) is not None


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _signature_matches(expected: str, signature: object) -> bool:
    if not isinstance(signature, str) or not signature:
        return False
    if len(signature) > SIGNATURE_MAX_LENGTH or not signature.isascii():
        return False
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))


def _timestamp(value: Any, operation: str) -> datetime | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value < MAX_TIMESTAMP
    ):
        raise _malformed(operation)
    return datetime.fromtimestamp(value, timezone.utc)


def _read_limited(response: httpx.Response) -> bytes | None:
    """Read a streamed body, or return None as soon as it exceeds MAX_RESPONSE_BYTES."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _malformed(operation: str) -> PaymentProviderError:
    logger.warning(
        "razorpay_request_failed",
        extra={"operation": operation, "reason": "malformed_response"},
    )
    return PaymentProviderError("Razorpay returned an unexpected response.")


class RazorpayProvider:
    """The Razorpay implementation of PaymentProvider."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        webhook_secret: str | None = None,
        timeout_seconds: float = 15.0,
        total_count: int = 60,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(key_id, str) or not KEY_ID_PATTERN.fullmatch(key_id):
            raise BillingNotConfiguredError(
                "RAZORPAY_KEY_ID must look like rzp_test_... or rzp_live_...."
            )
        if not isinstance(key_secret, str) or not key_secret.strip():
            raise BillingNotConfiguredError("RAZORPAY_KEY_SECRET is not set.")
        if webhook_secret is not None and not webhook_secret.strip():
            webhook_secret = None
        if timeout_seconds <= 0:
            raise BillingNotConfiguredError(
                "RAZORPAY_TIMEOUT_SECONDS must be positive."
            )
        if isinstance(total_count, bool) or total_count < 1:
            raise BillingNotConfiguredError(
                "RAZORPAY_SUBSCRIPTION_TOTAL_COUNT must be at least 1."
            )
        self.key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(CONNECT_TIMEOUT_SECONDS, timeout_seconds)
        )
        self._total_count = total_count
        self._transport = transport

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> "RazorpayProvider":
        """Build the provider, or raise BillingNotConfiguredError naming what is missing."""
        key_id = settings.RAZORPAY_KEY_ID
        key_secret = settings.RAZORPAY_KEY_SECRET
        if key_id is None or key_secret is None:
            missing = [
                name
                for name, value in (
                    ("RAZORPAY_KEY_ID", key_id),
                    ("RAZORPAY_KEY_SECRET", key_secret),
                )
                if value is None
            ]
            raise BillingNotConfiguredError(f"Missing settings: {', '.join(missing)}.")
        webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
        return cls(
            key_id=key_id,
            key_secret=key_secret.get_secret_value(),
            webhook_secret=(
                webhook_secret.get_secret_value() if webhook_secret else None
            ),
            timeout_seconds=settings.RAZORPAY_TIMEOUT_SECONDS,
            total_count=settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT,
            transport=transport,
        )

    # --- API calls ----------------------------------------------------------

    def create_subscription(self, user: User, plan: Plan) -> CheckoutSession:
        if not plan.is_paid:
            raise ValueError("Only paid plans have a provider subscription.")
        if not is_provider_id(plan.razorpay_plan_id):
            raise BillingNotConfiguredError(
                f"The Razorpay plan id of the {plan.code.value} plan is not set."
            )
        payload = {
            "plan_id": plan.razorpay_plan_id,
            "total_count": self._total_count,
            "quantity": 1,
            "customer_notify": 1,
            "notes": {"user_id": str(user.id), "plan_code": plan.code.value},
        }
        data = self._request("create_subscription", "POST", "/subscriptions", payload)
        subscription = self._parse_subscription(data, "create_subscription")
        if subscription.plan_id is not None and (
            subscription.plan_id != plan.razorpay_plan_id
        ):
            raise _malformed("create_subscription")
        return CheckoutSession(
            provider=self.name,
            key_id=self.key_id,
            provider_subscription_id=subscription.provider_subscription_id,
        )

    def fetch_subscription(self, provider_subscription_id: str) -> ProviderSubscription:
        self._check_id(provider_subscription_id)
        data = self._request(
            "fetch_subscription",
            "GET",
            f"/subscriptions/{provider_subscription_id}",
        )
        return self._parse_subscription(
            data, "fetch_subscription", expected_id=provider_subscription_id
        )

    def cancel_subscription(
        self, provider_subscription_id: str, *, at_cycle_end: bool = True
    ) -> ProviderSubscription:
        self._check_id(provider_subscription_id)
        data = self._request(
            "cancel_subscription",
            "POST",
            f"/subscriptions/{provider_subscription_id}/cancel",
            {"cancel_at_cycle_end": 1 if at_cycle_end else 0},
        )
        return self._parse_subscription(
            data, "cancel_subscription", expected_id=provider_subscription_id
        )

    # --- Signatures ---------------------------------------------------------

    def verify_payment_signature(
        self, *, payment_id: str, subscription_id: str, signature: str
    ) -> bool:
        if not is_provider_id(payment_id) or not is_provider_id(subscription_id):
            return False
        expected = _hmac_hex(
            self._key_secret, f"{payment_id}|{subscription_id}".encode("ascii")
        )
        return _signature_matches(expected, signature)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        if self._webhook_secret is None:
            logger.warning(
                "razorpay_webhook_secret_missing",
                extra={"reason": "RAZORPAY_WEBHOOK_SECRET is not set."},
            )
            return False
        if not isinstance(body, bytes):
            return False
        return _signature_matches(_hmac_hex(self._webhook_secret, body), signature)

    # --- Helpers ------------------------------------------------------------

    @staticmethod
    def _check_id(provider_subscription_id: str) -> None:
        if not is_provider_id(provider_subscription_id):
            raise ValueError("Invalid provider subscription id.")

    def _request(
        self,
        operation: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=RAZORPAY_API_BASE_URL,
                auth=httpx.BasicAuth(self.key_id, self._key_secret),
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                with client.stream(
                    method,
                    path,
                    json=payload,
                    headers={"Accept": "application/json"},
                ) as response:
                    status_code = response.status_code
                    is_success = response.is_success
                    # The body of an error answer is never read.
                    content = _read_limited(response) if is_success else b""
        except httpx.TimeoutException:
            logger.warning(
                "razorpay_request_failed",
                extra={"operation": operation, "reason": "timeout"},
            )
            raise PaymentProviderTimeoutError(
                "Razorpay did not respond in time."
            ) from None
        except httpx.HTTPError as exc:
            logger.warning(
                "razorpay_request_failed",
                extra={
                    "operation": operation,
                    "reason": "network_error",
                    "error_type": type(exc).__name__,
                },
            )
            raise PaymentProviderError("Razorpay could not be reached.") from None

        if not is_success:
            logger.warning(
                "razorpay_request_failed",
                extra={
                    "operation": operation,
                    "reason": "http_error",
                    "status_code": status_code,
                },
            )
            raise PaymentProviderError(
                f"Razorpay answered with HTTP {status_code}.",
                status_code=status_code,
            )
        if content is None:
            raise _malformed(operation)
        try:
            data = json.loads(content)
        except (ValueError, RecursionError):
            raise _malformed(operation) from None
        if not isinstance(data, dict):
            raise _malformed(operation)
        return data

    def _parse_subscription(
        self,
        data: dict[str, Any],
        operation: str,
        *,
        expected_id: str | None = None,
    ) -> ProviderSubscription:
        subscription_id = data.get("id")
        status = data.get("status")
        raw_plan_id = data.get("plan_id")
        if not isinstance(subscription_id, str) or not is_provider_id(subscription_id):
            raise _malformed(operation)
        if expected_id is not None and subscription_id != expected_id:
            raise _malformed(operation)
        if not isinstance(status, str) or not STATUS_PATTERN.fullmatch(status.lower()):
            raise _malformed(operation)
        plan_id: str | None = None
        if raw_plan_id is not None:
            if not isinstance(raw_plan_id, str) or not is_provider_id(raw_plan_id):
                raise _malformed(operation)
            plan_id = raw_plan_id
        return ProviderSubscription(
            provider_subscription_id=subscription_id,
            status=status.lower(),
            plan_id=plan_id,
            current_start=_timestamp(data.get("current_start"), operation),
            current_end=_timestamp(data.get("current_end"), operation),
        )
