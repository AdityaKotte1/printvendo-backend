"""Standing up a kiosk in one call, and being told what is still missing.

Setting up a shop was eight requests in the right order across three surfaces,
and the order matters: CONFIGURED cannot be skipped, and a SOLD kiosk whose
owner cannot collect must not reach LIVE. Doing it by hand meant knowing all of
that.

This does not *loosen* any of it. It climbs the same ladder through the same
services and stops where the rules say stop -- and then says plainly why, in
sentences an operator can act on, rather than leaving somebody to work out from
a stage name that they are waiting on an owner's Razorpay keys.
"""

from decimal import Decimal

import pytest

from app.modules.billing.models import Plan
from app.modules.identity.models import User
from app.modules.kiosks import KioskType, OnboardingStage, sheets_remaining
from app.provisioning import blocking_reasons, provision_kiosk

PRICES = {
    "bw_single": Decimal("2.00"),
    "bw_double": Decimal("3.00"),
    "color_single": Decimal("10.00"),
    "color_double": Decimal("20.00"),
}


@pytest.fixture
def admin(db_session) -> User:
    user = User(email="ops@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def plan(db_session) -> Plan:
    p = Plan(name="Pro", monthly_price=Decimal("1000.00"))
    db_session.add(p)
    db_session.flush()
    return p


# ── a platform kiosk goes all the way ───────────────────────────────────────


def test_a_platform_kiosk_reaches_live_in_one_call(db_session, admin):
    """Our own keys always work, so nothing is waiting on anybody."""
    result = provision_kiosk(
        db_session,
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert result.kiosk.onboarding_stage is OnboardingStage.LIVE
    assert result.blocked_by == []


def test_it_comes_with_paper_and_a_printer_slot(db_session, admin):
    """A kiosk that is LIVE with an empty tray and no way to enrol a machine is
    live in name only."""
    result = provision_kiosk(
        db_session,
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert sheets_remaining(db_session, result.kiosk) > 0
    assert result.enrolment_code.startswith("dve_")


def test_the_prices_are_the_ones_that_were_asked_for(db_session, admin):
    result = provision_kiosk(
        db_session,
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert result.kiosk.price_bw_single == Decimal("2.00")
    assert result.kiosk.price_color_double == Decimal("20.00")


def test_a_platform_kiosk_takes_wallet_money(db_session, admin):
    """Where the platform collects, balance may be spent -- and a kiosk that
    cannot take it is a kiosk no student with credit can use."""
    result = provision_kiosk(
        db_session,
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert result.kiosk.accepts_wallet is True


# ── a sold kiosk stops where the rules say stop ─────────────────────────────


def test_a_sold_kiosk_stops_short_of_live(db_session, admin):
    """It has no owner yet, so nobody can collect at it. Reaching LIVE would
    mean the platform quietly collecting a shop's takings."""
    result = provision_kiosk(
        db_session,
        name="Corner Shop",
        kiosk_type=KioskType.SOLD,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert result.kiosk.onboarding_stage is not OnboardingStage.LIVE


def test_it_says_what_it_is_waiting_for(db_session, admin):
    """In sentences. An operator reading "configured" cannot tell whether they
    are waiting on an owner, a subscription or a set of keys."""
    result = provision_kiosk(
        db_session,
        name="Corner Shop",
        kiosk_type=KioskType.SOLD,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert result.blocked_by
    assert any("owner" in reason.lower() for reason in result.blocked_by)


def test_a_sold_kiosk_never_takes_wallet_money(db_session, admin):
    """Top-ups are held by Printvendo; spending them where the owner collects
    would have us keep the cash while the shop prints for free."""
    result = provision_kiosk(
        db_session,
        name="Corner Shop",
        kiosk_type=KioskType.SOLD,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert result.kiosk.accepts_wallet is False


def test_the_owner_is_invited_rather_than_attached(db_session, admin):
    """Consent, the same as everywhere else: an invitation is sent and the
    person accepts it. Attaching an address to a shop without asking is how
    somebody ends up owning a kiosk they have never heard of."""
    result = provision_kiosk(
        db_session,
        name="Corner Shop",
        kiosk_type=KioskType.SOLD,
        prices=PRICES,
        owner_email="shopkeeper@example.com",
        actor_user_id=admin.id,
    )

    assert result.owner_invite_token is not None
    assert any("invitation" in reason.lower() for reason in result.blocked_by)


# ── what is still missing ───────────────────────────────────────────────────


def test_a_live_platform_kiosk_is_blocked_by_nothing(db_session, admin):
    result = provision_kiosk(
        db_session,
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    assert blocking_reasons(db_session, result.kiosk) == []


def test_the_reasons_come_in_the_order_they_can_be_answered(db_session, admin, plan):
    """Three things stand between a sold kiosk and its first sale, and they are
    not independent: until somebody owns the shop there is nobody whose
    subscription or keys could be missing. Listing all three at once would send
    an operator chasing an owner who does not exist for keys that cannot exist.
    """
    from app.modules.identity.models import User
    from app.modules.kiosks import accept_invite

    result = provision_kiosk(
        db_session,
        name="Corner Shop",
        kiosk_type=KioskType.SOLD,
        prices=PRICES,
        owner_email="shopkeeper@example.com",
        actor_user_id=admin.id,
    )

    # Nobody has accepted yet: one thing outstanding, and it is not the keys.
    assert blocking_reasons(db_session, result.kiosk) == [
        reason for reason in blocking_reasons(db_session, result.kiosk) if "owner" in reason.lower()
    ]

    shopkeeper = User(email="shopkeeper@example.com", hashed_password="x")
    db_session.add(shopkeeper)
    db_session.flush()
    accept_invite(db_session, result.owner_invite_token, user=shopkeeper)

    # Now there is somebody to be missing things, and both are named.
    reasons = " ".join(blocking_reasons(db_session, result.kiosk)).lower()
    assert "subscription" in reasons
    assert "razorpay" in reasons


def test_two_kiosks_cannot_share_a_name(db_session, admin):
    """The same refusal as `create_kiosk`, not a quieter one."""
    from app.core.errors import Conflict

    provision_kiosk(
        db_session,
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        prices=PRICES,
        actor_user_id=admin.id,
    )

    with pytest.raises(Conflict):
        provision_kiosk(
            db_session,
            name="Campus Print",
            kiosk_type=KioskType.PLATFORM,
            prices=PRICES,
            actor_user_id=admin.id,
        )


def test_a_price_outside_the_band_is_refused(db_session, admin):
    """Provisioning goes through `set_pricing`, so every pricing rule applies --
    including that a double-sided sheet cannot cost less than a single-sided
    one."""
    from app.core.errors import BadRequest

    with pytest.raises(BadRequest):
        provision_kiosk(
            db_session,
            name="Backwards Shop",
            kiosk_type=KioskType.PLATFORM,
            prices={**PRICES, "bw_double": Decimal("1.00")},
            actor_user_id=admin.id,
        )


def test_a_kiosk_with_no_prices_given_uses_the_platform_defaults(db_session, admin):
    """Setting nothing is not the same as setting nothing *down*.

    `set_pricing` refuses an empty change, rightly -- a request to set no prices
    is a mistake. So provisioning does not call it, and the kiosk charges the
    platform default that `effective_prices` fills in.
    """
    from app.modules.kiosks import effective_prices

    result = provision_kiosk(
        db_session,
        name="Default Prices Shop",
        kiosk_type=KioskType.PLATFORM,
        prices={},
        actor_user_id=admin.id,
    )

    assert result.kiosk.onboarding_stage is OnboardingStage.LIVE
    assert effective_prices(result.kiosk)["bw_single"] > 0
