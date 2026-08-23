"""Request-scoped dependencies shared by every audience.

There is exactly one place that turns a bearer token into a user, and exactly
one that checks a role. The backend being replaced had a per-router auth
dependency, which is how /owner/* ended up admin-only with a "DO NOT LOOSEN"
comment instead of a check -- there was no single place to put the check.
"""

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.bus import Bus, flush_wakes, redis_bus
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.db import get_session_factory
from app.core.errors import Forbidden, Unauthorized
from app.core.notifier import BrevoNotifier, LoggingNotifier, Notifier
from app.core.security import TokenError, TokenType, decode_token
from app.modules.billing import Subscription, activate_subscription, price_band_for
from app.modules.identity import User
from app.modules.identity import repository as repo
from app.modules.identity.roles import Role
from app.modules.kiosks import (
    BandSource,
    BillingCheck,
    Kiosk,
    KioskDevice,
    PriceBand,
    Scope,
    UnboundedBand,
    authenticate_device,
    consume_paper,
    kiosk_scope,
)
from app.modules.ops import AlertSeverity, raise_alert
from app.modules.orders import (
    apply_payment_refund,
    document_is_in_an_order,
    refresh_order_state,
    settle_paid_order,
)
from app.modules.payments import (
    HttpRazorpay,
    Payment,
    RazorpayGateway,
    Refund,
    RefundSink,
    Settlement,
)
from app.modules.payments.gate import GateBilling
from app.modules.printing import DocumentStore, DocumentUse, TaskOutcome
from app.modules.wallet import EntryKind, credit

logger = logging.getLogger(__name__)

NOT_SIGNED_IN = "You need to sign in to do that."
NOT_ALLOWED = "You do not have access to that."


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


def get_secret(settings: Annotated[Settings, Depends(get_settings_from_app)]) -> str:
    return settings.JWT_SECRET_KEY


def get_db(
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> Iterator[Session]:
    """One transaction per request: commit on success, roll back on any error.

    A handler that raises must not leave a half-written change behind, and
    nothing should have to remember to call commit.
    """
    session = get_session_factory(settings.DATABASE_URL)()
    try:
        yield session
        session.commit()
        # Only now: a device woken before the commit would ask for work, not see
        # it, and have spent its one notification. Nothing here can fail the
        # request -- it has already committed -- and a kiosk that misses a wake
        # simply polls, which is what every device did before the socket
        # existed.
        flush_wakes(session, lambda: redis_bus(settings.REDIS_URL))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_notifier(
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    db: Annotated[Session, Depends(get_db)],
) -> Notifier:
    """How out-of-band messages leave the system.

    Brevo when a key is configured, the logging one otherwise -- so a developer
    with no key still completes a verification flow by reading the log, and
    production does not quietly do the same thing while believing it sends mail.

    A failed send raises an admin alert. `BrevoNotifier` cannot do that itself:
    core may not import a bounded context. This is the composition root, which
    can, and an invitation that never arrived is exactly the kind of silent
    failure the alerts table exists for -- a shop waits for an email nobody
    knows was lost.
    """
    if not settings.BREVO_API_KEY.strip():
        return LoggingNotifier()

    def report(kind: str, email: str) -> None:
        # Deduplicated on the kind alone rather than on the address: when the
        # provider is down every send fails, and one alert saying "email is not
        # going out" is what an operator needs. A thousand rows naming a
        # thousand recipients is the wall of identical notifications that made
        # the old backend's console unreadable.
        raise_alert(
            db,
            kind="email.send.failed",
            severity=AlertSeverity.CRITICAL,
            summary=(
                "Email is not being delivered. Invitations and password "
                "resets are not arriving."
            ),
            dedupe_key="email.send.failed",
            detail={"last_failure": kind},
        )

    return BrevoNotifier(
        api_key=settings.BREVO_API_KEY,
        app_base_url=settings.APP_BASE_URL,
        sender_email=settings.MAIL_FROM_EMAIL,
        sender_name=settings.MAIL_FROM_NAME,
        on_failure=report,
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized(NOT_SIGNED_IN)
    return token


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
) -> User:
    token = _bearer_token(request)

    try:
        claims = decode_token(token, TokenType.ACCESS, secret)
    except TokenError as exc:
        raise Unauthorized(NOT_SIGNED_IN) from exc

    # repo.get_by_public_id refuses an id of the wrong kind and any inactive
    # account, so a kiosk id in `sub` cannot resolve to a user.
    user = repo.get_by_public_id(db, claims.subject)
    if user is None:
        raise Unauthorized(NOT_SIGNED_IN)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(role: Role):
    """Dependency factory: refuse anyone who does not hold `role`."""

    def _guard(
        user: CurrentUser,
        db: Annotated[Session, Depends(get_db)],
    ) -> User:
        if role not in repo.roles_of(db, user.id):
            raise Forbidden(NOT_ALLOWED)
        return user

    return _guard


def require_any_role(*roles: Role):
    """Refuse anyone holding none of `roles`.

    Exists so an audience can be "an owner, or an admin acting as one" without
    a route repeating the disjunction. Admin is a wider *scope* here rather than
    a bypass: it gets an owner through the door, and `kiosk_scope` still decides
    which kiosks are behind it.

    Applied to a whole router rather than to each route, so a route added later
    is guarded by existing. `/v1/owner/kiosks` and `/v1/refiller/kiosks` were
    guarded by scope alone -- any signed-in student reached the handler and got
    an empty list, which leaked nothing and made the authz matrix's claim about
    them untrue. `tests/authz/test_matrix_enforced.py` found them.
    """

    def _guard(
        user: CurrentUser,
        db: Annotated[Session, Depends(get_db)],
    ) -> User:
        held = repo.roles_of(db, user.id)
        if not held & set(roles):
            raise Forbidden(NOT_ALLOWED)
        return user

    return _guard


def get_kiosk_scope(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> Scope:
    """Which kiosks this caller may touch.

    Injected rather than computed inside handlers so no route can accidentally
    query kiosks without one -- the repository has no unscoped read.
    """
    return kiosk_scope(db, user)


KioskScope = Annotated[Scope, Depends(get_kiosk_scope)]


def get_billing_check() -> BillingCheck:
    """Whether a kiosk's owner can collect into their own account.

    This is `payments.gate.kiosk_payment_gate` behind the protocol the kiosks
    module declares. Deliberately the same function the payment path uses: if
    the LIVE gate and the payment routing could disagree, a kiosk could sit LIVE
    while unable to take a rupee.
    """
    return GateBilling()


def get_current_device(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> KioskDevice:
    """The kiosk machine making this request.

    The token *is* the kiosk. Nothing the device sends names one, so there is no
    printer id in a URL for a handler to trust -- which is how one shop's Pi
    could fetch another shop's job file in the old backend.
    """
    return authenticate_device(db, request.headers.get("X-Device-Token"))


CurrentDevice = Annotated[KioskDevice, Depends(get_current_device)]


class OrderedDocuments:
    """Whether an order is still counting on a document.

    The adapter lives here, at the composition root, for the reason
    `KioskPaperLedger` does: printing declares what it needs (`DocumentUse`),
    orders can answer it, and neither has to import the other.
    """

    def is_referenced(self, db: Session, document_id: int) -> bool:
        return document_is_in_an_order(db, document_id)


class OrderProgress:
    """Tells the order behind a task that the task has finished.

    The adapter lives here for the reason `KioskPaperLedger` does: printing
    declares what it needs (`TaskOutcome`), orders knows what a finished print
    means for an order, and neither imports the other.
    """

    def task_finished(self, db: Session, task) -> None:
        refresh_order_state(db, document_id=task.document_id, kiosk_id=task.kiosk_id)


def get_task_outcome() -> TaskOutcome:
    """What a finished print means for whatever paid for it."""
    return OrderProgress()


def get_document_use() -> DocumentUse:
    """Injected rather than constructed in the route, so a test can delete a
    document without standing up an order to prove it is not in one."""
    return OrderedDocuments()


def get_document_store() -> DocumentStore:
    """Where uploaded files live. A dependency so a test can point it at a
    temporary directory without touching the environment."""
    return DocumentStore.from_settings()


class KioskPaperLedger:
    """Deducts a finished print's paper from **one** kiosk's tray.

    The adapter lives here, at the composition root, rather than in either
    module: printing declares what it needs (`PaperLedger`), kiosks owns the
    tray, and neither has to import the other.

    It is constructed around the kiosk whose device is reporting, so paper can
    only ever leave that tray. The id check can never fire through the routes as
    written -- the task was fetched for this kiosk -- which is exactly why it is
    cheap to keep: it is what makes that still true after the next change.
    """

    def __init__(self, kiosk: Kiosk) -> None:
        self._kiosk = kiosk

    def consume(
        self,
        db: Session,
        kiosk_id: int,
        *,
        predicted_sheets: int,
        actual_sheets: int | None,
        reference: str,
    ) -> None:
        if kiosk_id != self._kiosk.id:
            raise Forbidden("That print job belongs to a different kiosk.")
        consume_paper(
            db,
            self._kiosk,
            predicted_sheets=predicted_sheets,
            actual_sheets=actual_sheets,
            reference=reference,
        )


class SubscriptionBand:
    """The real `BandSource`: what this kiosk's owner's plan lets them charge.

    Replaces `PlatformBand`, which returned an unbounded band as a stand-in
    until billing existed. Billing exists, so an owner can no longer set any
    price at all.

    The adapter lives here for the same reason `KioskPaperLedger` does. Kiosks
    declares what it needs (`BandSource`); billing answers for a *user*, knowing
    nothing about kiosks; and joining "who owns this kiosk" to "what is that
    owner paying for" is the one thing the composition root is for.

    A kiosk with no owner -- a PLATFORM install -- is unbounded, because its
    prices are the platform's to set. That is the same direction billing fails
    in, and it is safe for the same reason: a SOLD or SAAS kiosk cannot reach
    LIVE without an active subscription, so the unbounded case is unreachable
    for exactly the kiosks a band exists to constrain.
    """

    def band_for(self, db: Session, kiosk: Kiosk) -> PriceBand:
        from app.modules.kiosks import repository as kiosk_repo

        owner = kiosk_repo.owner_of(db, kiosk)
        if owner is None:
            return UnboundedBand

        band = price_band_for(db, owner.id)
        return PriceBand(
            floor_bw=band.floor_bw,
            ceiling_bw=band.ceiling_bw,
            floor_color=band.floor_color,
            ceiling_color=band.ceiling_color,
        )


def get_band_source() -> BandSource:
    """The price band an owner must stay within."""
    return SubscriptionBand()


class OrderRefundSink:
    """Tells the orders module that money it was paid has gone back.

    The adapter lives here for the same reason `KioskPaperLedger` does: payments
    declares what it needs (`RefundSink`), orders owns the state, and payments
    must not import orders -- it would be a cycle, since orders already calls
    the payment gate. The `payments-does-not-know-what-it-paid-for` contract
    fails the build if anyone shortens the path.

    Stateless, so one instance serves every request.
    """

    def on_refund(self, db: Session, payment: Payment, refund: Refund) -> None:
        apply_payment_refund(
            db,
            order_id=payment.order_id,
            refunded_inr=payment.refunded_inr,
            paid_inr=payment.amount_inr,
        )


def get_refund_sink() -> RefundSink:
    """What a refund means for whatever was paid for.

    A real implementation rather than a placeholder: `OrderState.REFUNDED` was
    a value the enum could hold and nothing ever set, which is the legacy
    audit's `REFUND_PENDING` in a new place.
    """
    return OrderRefundSink()


class WebhookSettlement:
    """What a captured payment means, resolved at the composition root.

    Payments must not import orders, wallet or billing -- it would make the
    dependency graph cyclic and the modules inseparable -- so `handle_webhook`
    calls this protocol instead and something above all four supplies it.

    The branch taken comes from `payment.kind`, a column written when the
    checkout was opened. That is the whole difference from the three-webhook
    arrangement being replaced, where the purpose of a payment was inferred from
    whichever endpoint the delivery happened to arrive at.
    """

    def settle_print_order(self, db: Session, payment: Payment) -> None:
        settle_paid_order(
            db,
            order_id=payment.order_id,
            reference=payment.razorpay_payment_id,
            now=payment.captured_at,
        )

    def settle_wallet_topup(self, db: Session, payment: Payment) -> None:
        # The razorpay payment id is the ledger reference, and wallet references
        # are unique per wallet -- so a webhook delivered three times credits
        # once, decided by the index rather than by a "have we seen this" query
        # that two concurrent deliveries can both answer no to.
        credit(
            db,
            user_id=payment.user_id,
            amount=payment.amount_inr,
            kind=EntryKind.TOPUP,
            reference=payment.razorpay_payment_id or payment.public_id,
        )

    def settle_subscription(self, db: Session, payment: Payment) -> None:
        """Put the subscription this payment bought in force.

        The link is `payment.subscription_id`, written when the checkout opened
        -- the same shape as `order_id` for a print. Re-deriving it from the
        payer would be wrong the first time an admin buys on somebody's behalf.

        Idempotent, because it has to be: the webhook and the browser coming
        back settle the same payment, and `activate_subscription` returns an
        already-active subscription untouched rather than extending its term a
        second time.

        A payment with nothing attached is logged rather than raised. The money
        has arrived and is recorded; what it was for is a question for a person,
        and throwing here would make Razorpay retry a delivery that will never
        succeed.
        """
        if payment.subscription_id is None:
            logger.error(
                "subscription payment %s captured with nothing to activate",
                payment.public_id,
            )
            return

        subscription = db.get(Subscription, payment.subscription_id)
        if subscription is None:
            logger.error(
                "subscription payment %s names a subscription that does not exist",
                payment.public_id,
            )
            return

        activate_subscription(db, subscription)


def get_settlement() -> Settlement:
    """What the Razorpay webhook hands a captured payment to."""
    return WebhookSettlement()


def get_secret_box(
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> SecretBox:
    """The key that decrypts third-party secrets at rest.

    A dependency rather than a module-level singleton so a test can hand over a
    throwaway key, and so importing this module never requires the real one.
    """
    return SecretBox(settings.SECRETS_ENCRYPTION_KEY)


def get_razorpay(
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> RazorpayGateway:
    """The gateway a real student's payment goes through.

    A dependency because that is what lets a test substitute the protocol and
    exercise the whole route -- signature check included -- without a network.
    The alternative, constructing a client inside the handler, is what makes the
    security-critical part of a payment path the bit nobody tests.
    """
    return HttpRazorpay(timeout=settings.RAZORPAY_TIMEOUT_SECONDS)


def get_bus(
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> Bus:
    """How a worker tells one kiosk's device that there is work.

    Built per request rather than once at startup, and that is cheap: the
    synchronous client pools connections, and the subscribing client is created
    per socket because a pub/sub connection is stateful and must not be shared
    between two devices.

    A dependency so a test can hand over `fakeredis` and exercise the real
    class. Redis is not installed locally, and the operator confirmed that is a
    production concern rather than a local one -- the device API works by
    polling without it.
    """
    return redis_bus(settings.REDIS_URL)
