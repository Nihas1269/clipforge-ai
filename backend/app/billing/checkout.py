"""Starting, confirming and cancelling a paid subscription.

Every operation works on the authenticated user's own subscription row (one
per user, see app/models/billing.py), which is locked (`SELECT ... FOR
UPDATE` on PostgreSQL) for the duration of the operation, so concurrent
requests of the same user are serialized.

- **Checkout** creates a provider subscription for a paid plan and stores it
  as `pending`. The plan and its price come from the server's plan
  definitions; the client only names the plan. A pending checkout for the
  same plan is returned again instead of creating another one, after the
  provider confirms it is still awaiting payment for the configured plan id.
  One that has ended at the provider (expired, cancelled, halted, completed)
  or belongs to another plan id is replaced by a new checkout (one that could
  still charge, e.g. `authenticated`, is first cancelled at the provider
  immediately). One the provider reports as paid (`active` for the
  configured plan id with a valid period) is activated from that
  provider-side state, so a payment whose /verify callback was lost is not
  stranded; checkout then answers 409 `subscription_exists`. One reported in
  any other state (e.g. paused) is left as it is (409 `checkout_unsettled`). A
  user whose subscription grants paid access, or is `past_due`, cannot start
  another. Any other (active, cancelled, expired) subscription with a
  provider id is fetched first and replaced only once the provider reports it
  ended (expired, cancelled, halted, completed); while it may still charge,
  checkout answers `409 subscription_exists`, because renewals do not move
  the local period until webhooks are processed.
- **Verify** checks the checkout callback's payment signature and then asks
  the provider for the subscription's state. Only a provider status of
  `active` for the expected plan, with a valid period, makes the local
  subscription `active`; the signature alone never does. Repeating a verify
  for an already active or cancelled subscription changes nothing.
- **Cancel** cancels at the provider at the end of the billing period and
  marks the subscription `cancelled`, which keeps paid access until
  `current_period_end` (see app/billing/service.py). Cancelling again changes
  nothing. If the provider refuses the cancel because the subscription is
  already cancelled there, the local subscription is marked `cancelled` too.
  A `pending` subscription that the provider reports as paid is activated
  from that state first and then cancelled (both saved together).

A failed provider call leaves the subscription unchanged, with one
exception: a user's first checkout commits a `pending` row without a
provider id before calling the provider (so concurrent first checkouts are
serialized), and that row stays if the call fails. It grants nothing and the
next checkout reuses it.

A `cancelled` subscription keeps paid access until `current_period_end`, so
checkout answers `409 subscription_exists` until that period has ended.

Log events carry
user and subscription ids, never provider ids, signatures or secrets.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing.plans import PlanCode, get_plans
from app.billing.providers.base import (
    BillingNotConfiguredError,
    CheckoutSession,
    PaymentProvider,
    PaymentProviderError,
    ProviderSubscription,
)
from app.billing.service import grants_paid_access
from app.core.config import Settings
from app.models.billing import Subscription, SubscriptionStatus
from app.models.user import User

logger = logging.getLogger(__name__)

# Provider statuses that grant no paid access yet: `created` and
# `authenticated` before the first charge, and `pending` while Razorpay
# retries a failed charge.
PROVIDER_PENDING_STATUSES = frozenset({"created", "authenticated", "pending"})
PROVIDER_ACTIVE_STATUS = "active"
# Provider statuses after which the subscription can never be paid for.
PROVIDER_TERMINAL_STATUSES = frozenset({"cancelled", "completed", "expired", "halted"})
# Statuses the provider reports for a subscription that is already cancelled.
PROVIDER_CANCELLED_STATUSES = frozenset({"cancelled", "completed", "expired"})


class BillingError(Exception):
    """A billing request that cannot be carried out; shown to the client."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _locked_subscription(db: Session, user: User) -> Subscription | None:
    return db.scalars(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def _lock_or_create_subscription(
    db: Session, user: User, plan_code: PlanCode, provider: str
) -> Subscription:
    subscription = _locked_subscription(db, user)
    if subscription is not None:
        return subscription
    # A pending placeholder without a provider id; it grants nothing. The
    # unique user_id decides between concurrent first checkouts.
    db.add(
        Subscription(
            user_id=user.id,
            plan_code=plan_code.value,
            status=SubscriptionStatus.PENDING,
            provider=provider,
            provider_subscription_id=None,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    subscription = _locked_subscription(db, user)
    if subscription is None:
        raise RuntimeError("The subscription row could not be created.")
    return subscription


def _log_extra(user: User, subscription: Subscription) -> dict[str, str]:
    return {"user_id": str(user.id), "subscription_id": str(subscription.id)}


def _expected_plan_id(
    subscription: Subscription, settings: Settings | None
) -> str | None:
    """The configured Razorpay plan id of the subscription's paid plan, if any."""
    try:
        plan = get_plans(settings)[PlanCode(subscription.plan_code)]
    except ValueError:
        return None
    return plan.razorpay_plan_id if plan.is_paid else None


def _is_paid_at_provider(
    remote: ProviderSubscription, expected_plan_id: str | None
) -> bool:
    """True if the provider reports the expected plan active with a valid period."""
    return (
        expected_plan_id is not None
        and remote.status == PROVIDER_ACTIVE_STATUS
        and remote.plan_id == expected_plan_id
        and remote.current_start is not None
        and remote.current_end is not None
        and remote.current_end > remote.current_start
    )


def _activate(subscription: Subscription, remote: ProviderSubscription) -> None:
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.current_period_start = remote.current_start
    subscription.current_period_end = remote.current_end


def _cancel_abandoned_checkout(
    provider: PaymentProvider, provider_subscription_id: str
) -> None:
    """Cancel at once an unpaid checkout that is about to be replaced.

    An `authenticated` (or `pending`) subscription holds a payment mandate and
    could still charge, so it must not be left behind.
    """
    try:
        provider.cancel_subscription(provider_subscription_id, at_cycle_end=False)
    except PaymentProviderError as exc:
        # Refused because it ended meanwhile: nothing is left to cancel.
        if exc.status_code is None or not 400 <= exc.status_code < 500:
            raise
        remote = provider.fetch_subscription(provider_subscription_id)
        if remote.status not in PROVIDER_TERMINAL_STATUSES:
            raise


def start_checkout(
    db: Session,
    user: User,
    plan_code: PlanCode | str,
    provider: PaymentProvider,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> CheckoutSession:
    """Create (or return the existing) pending checkout for a paid plan."""
    plan = get_plans(settings)[PlanCode(plan_code)]
    if not plan.is_paid:
        raise BillingError(
            400, "plan_not_purchasable", "This plan does not need a checkout."
        )
    if plan.razorpay_plan_id is None:
        raise BillingNotConfiguredError(
            f"The Razorpay plan id of the {plan.code.value} plan is not set."
        )
    if now is None:
        now = datetime.now(timezone.utc)

    subscription = _lock_or_create_subscription(db, user, plan.code, provider.name)
    try:
        if (
            grants_paid_access(subscription, now)
            or subscription.status == SubscriptionStatus.PAST_DUE
        ):
            logger.info(
                "billing_checkout_rejected",
                extra={**_log_extra(user, subscription), "reason": "subscribed"},
            )
            raise BillingError(
                409, "subscription_exists", "You already have a subscription."
            )

        if (
            subscription.provider == provider.name
            and subscription.provider_subscription_id is not None
        ):
            # Whatever the local status, the provider subscription may still
            # be charging (e.g. an `active` row whose local period lapsed
            # because renewals are not synced yet). It is only replaced once
            # the provider reports it unpaid or ended.
            remote = provider.fetch_subscription(subscription.provider_subscription_id)
            if subscription.status != SubscriptionStatus.PENDING:
                if remote.status not in PROVIDER_TERMINAL_STATUSES:
                    logger.info(
                        "billing_checkout_rejected",
                        extra={
                            **_log_extra(user, subscription),
                            "reason": "provider_subscription_live",
                            "provider_status": remote.status,
                        },
                    )
                    raise BillingError(
                        409,
                        "subscription_exists",
                        "You already have a subscription.",
                    )
            elif (
                subscription.plan_code == plan.code.value
                and remote.status in PROVIDER_PENDING_STATUSES
                and remote.plan_id == plan.razorpay_plan_id
            ):
                # Idempotent: the same pending checkout is offered again.
                session = CheckoutSession(
                    provider=provider.name,
                    key_id=provider.key_id,
                    provider_subscription_id=subscription.provider_subscription_id,
                )
                db.commit()
                logger.info(
                    "billing_checkout_reused", extra=_log_extra(user, subscription)
                )
                return session
            if (
                remote.status not in PROVIDER_TERMINAL_STATUSES
                and remote.status not in PROVIDER_PENDING_STATUSES
            ):
                # Paid (or paused) at the provider: a new checkout could charge
                # twice. When the provider confirms the payment (e.g. the
                # browser never called /verify), the subscription is
                # activated here from that provider-side state.
                if subscription.plan_code == plan.code.value and _is_paid_at_provider(
                    remote, plan.razorpay_plan_id
                ):
                    _activate(subscription, remote)
                    db.commit()
                    logger.info(
                        "billing_subscription_activated",
                        extra={
                            **_log_extra(user, subscription),
                            "plan_code": subscription.plan_code,
                            "source": "checkout_sync",
                        },
                    )
                    raise BillingError(
                        409,
                        "subscription_exists",
                        "Your payment was confirmed and your subscription is active.",
                    )
                logger.info(
                    "billing_checkout_rejected",
                    extra={
                        **_log_extra(user, subscription),
                        "reason": "checkout_unsettled",
                        "provider_status": remote.status,
                    },
                )
                raise BillingError(
                    409,
                    "checkout_unsettled",
                    "Your previous checkout is still being processed. "
                    "Confirm it or try again later.",
                )
            if remote.status in PROVIDER_PENDING_STATUSES:
                # Replaced while still able to charge (another plan id).
                _cancel_abandoned_checkout(
                    provider, subscription.provider_subscription_id
                )
            logger.info(
                "billing_checkout_replaced",
                extra={
                    **_log_extra(user, subscription),
                    "provider_status": remote.status,
                },
            )

        session = provider.create_subscription(user, plan)
        subscription.plan_code = plan.code.value
        subscription.status = SubscriptionStatus.PENDING
        subscription.provider = provider.name
        subscription.provider_subscription_id = session.provider_subscription_id
        subscription.current_period_start = None
        subscription.current_period_end = None
        try:
            db.commit()
        except IntegrityError:
            raise PaymentProviderError(
                "The provider returned a subscription id that is already in use."
            ) from None
    except Exception:
        db.rollback()
        raise

    logger.info(
        "billing_checkout_created",
        extra={**_log_extra(user, subscription), "plan_code": plan.code.value},
    )
    return session


def verify_checkout(
    db: Session,
    user: User,
    provider: PaymentProvider,
    *,
    payment_id: str,
    provider_subscription_id: str,
    signature: str,
    settings: Settings | None = None,
) -> Subscription:
    """Confirm a checkout callback and activate the subscription if paid.

    Returns the subscription; it stays `pending` while the provider has not
    reported the first payment yet.
    """
    subscription = _locked_subscription(db, user)
    try:
        if (
            subscription is None
            or subscription.provider != provider.name
            or subscription.provider_subscription_id is None
            or subscription.provider_subscription_id != provider_subscription_id
        ):
            logger.info(
                "billing_verify_unknown_subscription", extra={"user_id": str(user.id)}
            )
            raise BillingError(
                404, "subscription_not_found", "No checkout was found for this payment."
            )

        if not provider.verify_payment_signature(
            payment_id=payment_id,
            subscription_id=provider_subscription_id,
            signature=signature,
        ):
            logger.warning(
                "billing_payment_signature_invalid",
                extra=_log_extra(user, subscription),
            )
            raise BillingError(
                400, "invalid_signature", "The payment signature is invalid."
            )

        if subscription.status in (
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.CANCELLED,
        ):
            # A duplicate callback: the subscription was already confirmed.
            db.commit()
            logger.info(
                "billing_verify_duplicate", extra=_log_extra(user, subscription)
            )
            return subscription
        if subscription.status != SubscriptionStatus.PENDING:
            raise BillingError(
                409,
                "subscription_not_pending",
                "This checkout can no longer be confirmed.",
            )

        try:
            plan = get_plans(settings)[PlanCode(subscription.plan_code)]
        except ValueError:
            raise BillingError(
                409, "subscription_plan_mismatch", "The subscription's plan is unknown."
            ) from None
        if plan.razorpay_plan_id is None:
            # Without the expected plan id the provider's plan cannot be
            # checked, so nothing is activated.
            raise BillingNotConfiguredError(
                f"The Razorpay plan id of the {plan.code.value} plan is not set."
            )

        remote = provider.fetch_subscription(provider_subscription_id)
        if remote.plan_id != plan.razorpay_plan_id:
            logger.error(
                "billing_subscription_plan_mismatch",
                extra=_log_extra(user, subscription),
            )
            raise BillingError(
                409,
                "subscription_plan_mismatch",
                "The subscription does not match the selected plan.",
            )

        if remote.status in PROVIDER_PENDING_STATUSES:
            db.commit()
            logger.info(
                "billing_verify_pending",
                extra={
                    **_log_extra(user, subscription),
                    "provider_status": remote.status,
                },
            )
            return subscription
        if remote.status != PROVIDER_ACTIVE_STATUS:
            logger.info(
                "billing_verify_not_active",
                extra={
                    **_log_extra(user, subscription),
                    "provider_status": remote.status,
                },
            )
            raise BillingError(
                409,
                "subscription_not_active",
                "The subscription is not active at the payment provider.",
            )
        if (
            remote.current_start is None
            or remote.current_end is None
            or remote.current_end <= remote.current_start
        ):
            logger.warning(
                "billing_verify_invalid_period", extra=_log_extra(user, subscription)
            )
            raise PaymentProviderError("The provider reported no valid billing period.")

        _activate(subscription, remote)
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "billing_subscription_activated",
        extra={**_log_extra(user, subscription), "plan_code": subscription.plan_code},
    )
    return subscription


def cancel_subscription(
    db: Session,
    user: User,
    provider: PaymentProvider,
    *,
    settings: Settings | None = None,
) -> Subscription:
    """Cancel the user's active subscription at the end of the billing period."""
    subscription = _locked_subscription(db, user)
    try:
        if subscription is None:
            raise BillingError(
                404, "subscription_not_found", "You have no subscription."
            )
        if subscription.status == SubscriptionStatus.CANCELLED:
            db.commit()
            logger.info(
                "billing_cancel_duplicate", extra=_log_extra(user, subscription)
            )
            return subscription
        if (
            subscription.status == SubscriptionStatus.PENDING
            and subscription.provider == provider.name
            and subscription.provider_subscription_id is not None
        ):
            # A checkout paid at the provider whose /verify never arrived:
            # take the confirmed provider-side state so it can be cancelled.
            # It is saved together with the cancellation.
            remote = provider.fetch_subscription(subscription.provider_subscription_id)
            if _is_paid_at_provider(remote, _expected_plan_id(subscription, settings)):
                _activate(subscription, remote)
                logger.info(
                    "billing_subscription_activated",
                    extra={
                        **_log_extra(user, subscription),
                        "plan_code": subscription.plan_code,
                        "source": "cancel_sync",
                    },
                )
        if (
            subscription.status != SubscriptionStatus.ACTIVE
            or subscription.provider != provider.name
            or subscription.provider_subscription_id is None
        ):
            raise BillingError(
                409,
                "no_active_subscription",
                "You have no active subscription to cancel.",
            )

        try:
            provider.cancel_subscription(subscription.provider_subscription_id)
        except PaymentProviderError as exc:
            # The provider refuses to cancel a subscription that is already
            # cancelled (e.g. from its dashboard); confirm that and continue.
            if exc.status_code is None or not 400 <= exc.status_code < 500:
                raise
            remote = provider.fetch_subscription(subscription.provider_subscription_id)
            if remote.status not in PROVIDER_CANCELLED_STATUSES:
                raise
            logger.info(
                "billing_cancel_already_cancelled",
                extra={
                    **_log_extra(user, subscription),
                    "provider_status": remote.status,
                },
            )
        # The paid period is kept: a cancelled subscription grants access
        # until current_period_end.
        subscription.status = SubscriptionStatus.CANCELLED
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info("billing_subscription_cancelled", extra=_log_extra(user, subscription))
    return subscription
