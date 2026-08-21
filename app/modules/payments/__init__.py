"""The payments bounded context.

Whose Razorpay account collects, what it collected, and how money goes back.
Import from here, never from the submodules' internals -- the
`api-does-not-touch-orm-models` contract makes this the only legal way for the
api layer to name a `Payment` or a `Refund`.

Two things are decided exactly once in here and recorded rather than re-derived:
the **gate**'s answer about whose account collects, written onto the Payment
row at checkout, and the **destination** of a refund, which reads that answer
back. The backend being replaced re-derived routing in three services, and two
of them disagreeing is what sent student money to the wrong account.

There is deliberately no import of `orders`. A refund may close an order and
orders already imports this module, so the transition comes back through
`RefundSink`, wired at the composition root. The
`payments-does-not-know-what-it-paid-for` contract holds that line.
"""

from app.modules.payments.charges import (
    Collection,
    Credentials,
    RazorpayGateway,
    confirm_payment,
    credentials_for,
    credentials_for_payment,
    open_checkout,
    payment_for_razorpay_order,
    record_capture,
    record_failure,
    record_wallet_payment,
    webhook_secret_for,
)
from app.modules.payments.configs import (
    PaymentConfigView,
    decrypt_secret,
    decrypt_webhook_secret,
    get_config,
    has_usable_keys,
    request_change,
    review_change,
    set_keys,
    set_webhook_secret,
    view_config,
)
from app.modules.payments.gate import (
    GateBilling,
    Gateway,
    can_take_payment,
    kiosk_payment_gate,
    wallet_may_be_spent,
)
from app.modules.payments.models import (
    ChangeRequestStatus,
    KioskPaymentConfig,
    Payment,
    PaymentConfigChangeRequest,
    PaymentKind,
    PaymentSource,
    PaymentStatus,
    Refund,
    RefundDestination,
)
from app.modules.payments.razorpay import HttpRazorpay, RazorpayError
from app.modules.payments.refunds import (
    RefundSink,
    refund,
    refund_for_key,
    refund_for_razorpay_id,
    refunds_for,
)
from app.modules.payments.signatures import (
    verify_payment_signature,
    verify_webhook_signature,
)
from app.modules.payments.webhook import (
    Settlement,
    WebhookOutcome,
    handle_webhook,
)

__all__ = [
    "ChangeRequestStatus",
    "Collection",
    "Credentials",
    "GateBilling",
    "HttpRazorpay",
    "Gateway",
    "KioskPaymentConfig",
    "Payment",
    "PaymentConfigChangeRequest",
    "PaymentConfigView",
    "PaymentKind",
    "PaymentSource",
    "PaymentStatus",
    "RazorpayError",
    "RazorpayGateway",
    "Refund",
    "RefundDestination",
    "RefundSink",
    "Settlement",
    "WebhookOutcome",
    "can_take_payment",
    "confirm_payment",
    "credentials_for",
    "credentials_for_payment",
    "decrypt_secret",
    "decrypt_webhook_secret",
    "get_config",
    "handle_webhook",
    "has_usable_keys",
    "kiosk_payment_gate",
    "open_checkout",
    "payment_for_razorpay_order",
    "record_capture",
    "record_failure",
    "record_wallet_payment",
    "refund",
    "refund_for_key",
    "refund_for_razorpay_id",
    "refunds_for",
    "request_change",
    "review_change",
    "set_keys",
    "set_webhook_secret",
    "verify_payment_signature",
    "verify_webhook_signature",
    "view_config",
    "wallet_may_be_spent",
    "webhook_secret_for",
]
