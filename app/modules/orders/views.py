"""Reading an order back, with the ids a caller is allowed to see.

An `Order` row holds numeric foreign keys to a kiosk and to documents, and
numeric primary keys never leave the database. Resolving them to public ids
needs both other contexts, so it happens here -- once -- rather than in each
handler that renders an order.

There are deliberately no ORM relationships from `Order` to `Kiosk` or from
`OrderItem` to `Document`. A relationship is an import of the other module's
table, which the `orders-uses-surfaces-not-tables` contract forbids and which
would make the three contexts inseparable. The cost is this file; the benefit is
that orders can be read without knowing how kiosks stores anything.

The lookups are batched. One query for the kiosks and one for the documents
across the whole page of orders, rather than two per row -- the N+1 the old
backend's order list had, which is why it took seconds to open.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.kiosks import Kiosk
from app.modules.orders.models import Order, OrderState
from app.modules.printing import Document


@dataclass(frozen=True)
class OrderLineView:
    document_id: str | None
    filename: str | None
    kind: str
    colour: bool
    duplex: bool
    copies: int
    page_range: str | None
    page_count: int
    sheets: int
    amount_inr: Decimal


@dataclass(frozen=True)
class OrderView:
    id: str
    kiosk_id: str
    state: OrderState
    payment_method: str | None
    subtotal_inr: Decimal
    fee_inr: Decimal
    total_inr: Decimal
    expires_at: datetime | None
    paid_at: datetime | None
    refunded_at: datetime | None
    created_at: datetime
    items: list[OrderLineView]


def _public_ids(db: Session, orders: list[Order]) -> tuple[dict, dict]:
    """Kiosk and document public ids for a whole page, in two queries."""
    kiosk_ids = {o.kiosk_id for o in orders}
    document_ids = {
        item.document_id
        for o in orders
        for item in o.items
        if item.document_id is not None
    }

    kiosks = (
        dict(
            db.execute(
                select(Kiosk.id, Kiosk.public_id).where(Kiosk.id.in_(kiosk_ids))
            ).all()
        )
        if kiosk_ids
        else {}
    )
    documents = (
        {
            row[0]: (row[1], row[2])
            for row in db.execute(
                select(
                    Document.id, Document.public_id, Document.original_filename
                ).where(Document.id.in_(document_ids))
            ).all()
        }
        if document_ids
        else {}
    )
    return kiosks, documents


def _view(order: Order, kiosks: dict, documents: dict) -> OrderView:
    return OrderView(
        id=order.public_id,
        kiosk_id=kiosks.get(order.kiosk_id, ""),
        state=order.state,
        payment_method=(
            order.payment_method.value if order.payment_method is not None else None
        ),
        subtotal_inr=order.subtotal_inr,
        fee_inr=order.fee_inr,
        total_inr=order.total_inr,
        expires_at=order.expires_at,
        paid_at=order.paid_at,
        refunded_at=order.refunded_at,
        created_at=order.created_at,
        items=[
            OrderLineView(
                document_id=documents.get(item.document_id, (None, None))[0],
                # A document purged by retention leaves its row and its name, so
                # a student's order history does not develop holes.
                filename=documents.get(item.document_id, (None, None))[1],
                kind=item.kind.value,
                colour=item.colour,
                duplex=item.duplex,
                copies=item.copies,
                page_range=item.page_range,
                page_count=item.page_count,
                sheets=item.sheets,
                amount_inr=item.amount_inr,
            )
            for item in order.items
        ],
    )


def view_of(db: Session, order: Order) -> OrderView:
    kiosks, documents = _public_ids(db, [order])
    return _view(order, kiosks, documents)


def order_for(db: Session, *, user_id: int, public_id: str) -> Order | None:
    """This student's order, or None.

    Scoped by `user_id` in the query rather than fetched and then checked. A
    fetch-then-compare is a rule someone can forget to apply; a WHERE clause is
    not.
    """
    return db.execute(
        select(Order).where(Order.public_id == public_id, Order.user_id == user_id)
    ).scalar_one_or_none()


def orders_of(db: Session, *, user_id: int, limit: int = 50) -> list[OrderView]:
    """This student's orders, newest first."""
    orders = list(
        db.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
        ).scalars()
    )
    kiosks, documents = _public_ids(db, orders)
    return [_view(o, kiosks, documents) for o in orders]
