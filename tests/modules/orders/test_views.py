"""Reading a shop's takings back over a window.

The window is on `paid_at` rather than on when the order was created, so this
answers the same question `/v1/owner/earnings` answers and the two can be
reconciled. An export whose total disagrees with the dashboard above it is worse
than no export: somebody has to work out which one is lying.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper
from app.modules.orders.models import Order, OrderState, PaymentMethod
from app.modules.orders.views import paid_orders_at_kiosks

NOON = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def user(db_session) -> User:
    user = User(email="views@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(
        name="Views Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
        price_bw_single=Decimal("2.00"),
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=0))
    db_session.flush()
    return kiosk


def _order(db_session, user, kiosk, *, paid_at, total="10.00") -> Order:
    order = Order(
        user_id=user.id,
        kiosk_id=kiosk.id,
        state=OrderState.PAID if paid_at else OrderState.AWAITING_PAYMENT,
        payment_method=PaymentMethod.WALLET,
        subtotal_inr=Decimal(total),
        fee_inr=Decimal("0.00"),
        total_inr=Decimal(total),
        paid_at=paid_at,
    )
    db_session.add(order)
    db_session.flush()
    return order


def test_an_order_paid_inside_the_window_is_included(db_session, user, kiosk):
    order = _order(db_session, user, kiosk, paid_at=NOON)

    found = paid_orders_at_kiosks(
        db_session,
        kiosk_ids=[kiosk.id],
        since=NOON - timedelta(hours=1),
        until=NOON + timedelta(hours=1),
    )

    assert [view.id for view in found] == [order.public_id]


def test_an_order_paid_before_the_window_is_left_out(db_session, user, kiosk):
    _order(db_session, user, kiosk, paid_at=NOON - timedelta(days=2))

    found = paid_orders_at_kiosks(
        db_session, kiosk_ids=[kiosk.id], since=NOON - timedelta(hours=1)
    )

    assert found == []


def test_the_end_of_the_window_is_exclusive(db_session, user, kiosk):
    """The same boundary earnings uses, so a day range means one day."""
    _order(db_session, user, kiosk, paid_at=NOON)

    found = paid_orders_at_kiosks(db_session, kiosk_ids=[kiosk.id], until=NOON)

    assert found == []


def test_an_unpaid_order_is_not_in_the_takings(db_session, user, kiosk):
    _order(db_session, user, kiosk, paid_at=None)

    assert paid_orders_at_kiosks(db_session, kiosk_ids=[kiosk.id]) == []


def test_no_kiosks_means_nothing_rather_than_everything(db_session, user, kiosk):
    """The distinction between an owner with no shops seeing an empty page and
    seeing every order on the platform."""
    _order(db_session, user, kiosk, paid_at=NOON)

    assert paid_orders_at_kiosks(db_session, kiosk_ids=[]) == []


def test_the_newest_payment_comes_first(db_session, user, kiosk):
    older = _order(db_session, user, kiosk, paid_at=NOON - timedelta(hours=2))
    newer = _order(db_session, user, kiosk, paid_at=NOON)

    found = paid_orders_at_kiosks(db_session, kiosk_ids=[kiosk.id])

    assert [view.id for view in found] == [newer.public_id, older.public_id]


def test_the_limit_is_honoured(db_session, user, kiosk):
    for hour in range(3):
        _order(db_session, user, kiosk, paid_at=NOON - timedelta(hours=hour))

    assert len(paid_orders_at_kiosks(db_session, kiosk_ids=[kiosk.id], limit=2)) == 2
