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

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.db import get_session_factory
from app.core.errors import Forbidden, Unauthorized
from app.core.notifier import LoggingNotifier, Notifier
from app.core.security import TokenError, TokenType, decode_token
from app.modules.identity import User
from app.modules.identity import repository as repo
from app.modules.identity.roles import Role
from app.modules.kiosks import (
    BandSource,
    BillingCheck,
    Kiosk,
    KioskDevice,
    PlatformBand,
    Scope,
    authenticate_device,
    consume_paper,
    kiosk_scope,
)
from app.modules.orders import apply_payment_refund, settle_paid_order
from app.modules.payments import (
    HttpRazorpay,
    Payment,
    RazorpayGateway,
    Refund,
    RefundSink,
    Settlement,
)
from app.modules.payments.gate import GateBilling
from app.modules.printing import DocumentStore
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
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_notifier() -> Notifier:
    """How out-of-band messages leave the system.

    Overridden in tests, and replaced by a real provider when the ops work
    lands. Defined here rather than constructed inside a handler so both
    substitutions are a one-line dependency override.
    """
    return LoggingNotifier()


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


def get_band_source() -> BandSource:
    """The price band an owner must stay within.

    Overridden by the billing module. Until then it is unbounded -- failing
    open, unlike the billing check above, because a silly price is visible and
    reversible while a misrouted payment is neither.
    """
    return PlatformBand()


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
        # Unreachable today: nothing can open a SUBSCRIPTION checkout, because
        # billing has no purchase route yet. Recorded loudly rather than passed
        # over, so that adding the route without also adding activation shows up
        # as a logged error against real money instead of silence.
        logger.error(
            "subscription payment %s captured with nothing to activate",
            payment.public_id,
        )


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
