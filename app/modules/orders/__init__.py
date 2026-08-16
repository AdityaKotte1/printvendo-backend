"""The orders bounded context.

The aggregate that owns the transaction: one student, one kiosk, N documents,
one payment, N print tasks, committed together.

Orders is the one context that legitimately spans the others -- it needs the
kiosk's prices, the payment gate's answer, and printing's queue. It reaches them
through their **public surfaces only**, never their tables, which the
`orders-uses-surfaces-not-tables` contract enforces.
"""

from app.modules.orders.models import (
    SETTLED_ORDER_STATES,
    ItemKind,
    Order,
    OrderItem,
    OrderState,
    PaymentMethod,
)
from app.modules.orders.quotes import (
    FEE_CAP_INR,
    FEE_RATE,
    LineQuote,
    OrderQuote,
    gateway_fee,
    quote_line,
)
from app.modules.orders.service import (
    ORDER_LIFETIME,
    RequestedDocument,
    expire_stale_orders,
    mark_paid,
    place_order,
    sheets_available,
)

__all__ = [
    "FEE_CAP_INR",
    "FEE_RATE",
    "ORDER_LIFETIME",
    "SETTLED_ORDER_STATES",
    "ItemKind",
    "LineQuote",
    "Order",
    "OrderItem",
    "OrderQuote",
    "OrderState",
    "PaymentMethod",
    "RequestedDocument",
    "expire_stale_orders",
    "gateway_fee",
    "mark_paid",
    "place_order",
    "quote_line",
    "sheets_available",
]
