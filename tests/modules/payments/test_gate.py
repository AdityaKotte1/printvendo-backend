"""The gate, and the three things that depend on it agreeing.

The old backend derived this answer in three places and two of them drifted,
which is what caused the wallet money leak. These tests exist to pin the single
rule and to prove the dependants read it rather than re-deriving it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SecretBox
from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
from app.modules.identity.models import User
from app.modules.kiosks.enums import AssignmentRole, KioskType
from app.modules.kiosks.models import KioskAssignment
from app.modules.kiosks.registry import create_kiosk, set_accepts_wallet
from app.modules.payments.configs import set_keys
from app.modules.payments.gate import (
    GateBilling,
    Gateway,
    can_take_payment,
    kiosk_payment_gate,
    wallet_may_be_spent,
)

BOX = SecretBox(Fernet.generate_key().decode())


@pytest.fixture
def plan(db_session) -> Plan:
    p = Plan(name="Pro", monthly_price=Decimal("1800.00"))
    db_session.add(p)
    db_session.flush()
    return p


def _owner(db_session, email="owner@example.com") -> User:
    user = User(email=email, hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


def _kiosk(db_session, kiosk_type=KioskType.SOLD, owner=None, name="Shop"):
    kiosk = create_kiosk(db_session, name=name, kiosk_type=kiosk_type)
    db_session.flush()
    if owner is not None:
        db_session.add(
            KioskAssignment(
                kiosk_id=kiosk.id, user_id=owner.id, role=AssignmentRole.OWNER
            )
        )
        db_session.flush()
    return kiosk


def _subscribe(db_session, owner, plan, *, days=30, status=SubscriptionStatus.ACTIVE,
               free_until=None):
    sub = Subscription(
        user_id=owner.id,
        plan_id=plan.id,
        status=status,
        duration_months=1,
        monthly_price_charged=plan.monthly_price,
        total_amount=plan.monthly_price,
        starts_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=days),
        free_until=free_until,
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def _configure_keys(db_session, owner):
    set_keys(db_session, owner.id, key_id="rzp_live_x", key_secret="s", box=BOX)
    db_session.flush()


# ── platform kiosks ─────────────────────────────────────────────────────────


def test_a_platform_kiosk_always_uses_the_platform_gateway(db_session):
    """Our own keys are always configured, so this can never be CLOSED."""
    kiosk = _kiosk(db_session, KioskType.PLATFORM)
    assert kiosk_payment_gate(db_session, kiosk) is Gateway.PLATFORM_GATEWAY


def test_a_platform_kiosk_needs_no_owner_or_subscription(db_session):
    kiosk = _kiosk(db_session, KioskType.PLATFORM)
    assert can_take_payment(db_session, kiosk) is True


# ── owner-gateway kiosks: every way to be CLOSED ────────────────────────────


def test_a_sold_kiosk_with_no_owner_is_closed(db_session):
    """Production had exactly this: a SOLD kiosk whose takings quietly landed in
    the platform account, with no settlement mechanism to return them."""
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=None)
    assert kiosk_payment_gate(db_session, kiosk) is Gateway.CLOSED


def test_a_sold_kiosk_without_a_subscription_is_closed(db_session):
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    _configure_keys(db_session, owner)

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.CLOSED


def test_a_sold_kiosk_without_keys_is_closed(db_session, plan):
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    _subscribe(db_session, owner, plan)

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.CLOSED


def test_an_expired_subscription_closes_the_kiosk(db_session, plan):
    """D7: a lapsed owner's kiosk stops selling rather than quietly routing
    their students' money to us."""
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(db_session, owner, plan, days=-1)

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.CLOSED


def test_a_cancelled_subscription_closes_the_kiosk(db_session, plan):
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(db_session, owner, plan, status=SubscriptionStatus.CANCELLED)

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.CLOSED


def test_a_lapsed_kiosk_never_falls_back_to_the_platform(db_session, plan):
    """The failure mode that matters: falling back would mean holding a shop's
    takings with no way to give them back."""
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(db_session, owner, plan, days=-1)

    assert kiosk_payment_gate(db_session, kiosk) is not Gateway.PLATFORM_GATEWAY


# ── owner-gateway kiosks: open ──────────────────────────────────────────────


def test_a_fully_set_up_sold_kiosk_uses_the_owner_gateway(db_session, plan):
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(db_session, owner, plan)

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.OWNER_GATEWAY


def test_saas_behaves_the_same_as_sold(db_session, plan):
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SAAS, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(db_session, owner, plan)

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.OWNER_GATEWAY


def test_a_trial_opens_the_owner_gateway(db_session, plan):
    """D13: a trial is a money-routing lever, not a billing courtesy. A
    trialling owner collects real money into their own account."""
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SAAS, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(
        db_session,
        owner,
        plan,
        days=-30,  # the paid term has elapsed
        free_until=datetime.now(UTC) + timedelta(days=60),
    )

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.OWNER_GATEWAY


def test_an_expired_trial_closes_the_kiosk(db_session, plan):
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SAAS, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(
        db_session, owner, plan, days=-30, free_until=datetime.now(UTC) - timedelta(days=1)
    )

    assert kiosk_payment_gate(db_session, kiosk) is Gateway.CLOSED


def test_one_owners_subscription_does_not_open_anothers_kiosk(db_session, plan):
    paying = _owner(db_session, "paying@example.com")
    freeloader = _owner(db_session, "free@example.com")
    _configure_keys(db_session, paying)
    _subscribe(db_session, paying, plan)

    kiosk = _kiosk(db_session, KioskType.SOLD, owner=freeloader, name="Other Shop")
    assert kiosk_payment_gate(db_session, kiosk) is Gateway.CLOSED


# ── wallet eligibility reads the gate, it does not re-derive it ─────────────


def test_wallet_is_spendable_at_a_platform_kiosk(db_session):
    kiosk = _kiosk(db_session, KioskType.PLATFORM)
    set_accepts_wallet(db_session, kiosk, accepts_wallet=True)
    db_session.flush()

    assert wallet_may_be_spent(db_session, kiosk) is True


def test_wallet_is_not_spendable_where_the_owner_collects(db_session, plan):
    """Top-ups sit in Printvendo's account. Spending them where the owner
    collects means Printvendo keeps the cash and the owner prints for free --
    the exact shape of the original wallet money leak."""
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    _configure_keys(db_session, owner)
    _subscribe(db_session, owner, plan)

    kiosk.accepts_wallet = True  # even if someone forces the flag on
    db_session.flush()

    assert wallet_may_be_spent(db_session, kiosk) is False


def test_the_accepts_wallet_flag_can_only_restrict(db_session):
    """An operator may turn wallet off at a platform kiosk; turning it on
    cannot make it legal where the owner collects."""
    kiosk = _kiosk(db_session, KioskType.PLATFORM)
    kiosk.accepts_wallet = False
    db_session.flush()

    assert wallet_may_be_spent(db_session, kiosk) is False


def test_wallet_is_not_spendable_at_a_closed_kiosk(db_session):
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=None)
    kiosk.accepts_wallet = True
    db_session.flush()

    assert wallet_may_be_spent(db_session, kiosk) is False


# ── the kiosks module's billing check is this gate ──────────────────────────


def test_the_billing_check_agrees_with_the_gate(db_session, plan):
    """kiosks.onboarding asks a BillingCheck whether an owner can collect. It
    must be the same answer the payment path uses, or a kiosk could be LIVE
    while unable to take money."""
    owner = _owner(db_session)
    kiosk = _kiosk(db_session, KioskType.SOLD, owner=owner)
    check = GateBilling()

    assert check.owner_can_collect(db_session, kiosk) is False

    _configure_keys(db_session, owner)
    _subscribe(db_session, owner, plan)

    assert check.owner_can_collect(db_session, kiosk) is True
    assert kiosk_payment_gate(db_session, kiosk) is Gateway.OWNER_GATEWAY


def test_the_billing_check_is_false_for_a_platform_kiosk(db_session):
    """A platform kiosk has no owner collecting -- the gate answers
    PLATFORM_GATEWAY, and 'can the owner collect' is simply no."""
    kiosk = _kiosk(db_session, KioskType.PLATFORM)
    assert GateBilling().owner_can_collect(db_session, kiosk) is False
