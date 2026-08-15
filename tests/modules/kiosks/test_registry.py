from decimal import Decimal

import pytest

from app.core.errors import BadRequest, Conflict
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import KioskPaper
from app.modules.kiosks.registry import (
    change_type,
    create_kiosk,
    rename_kiosk,
    set_accepts_wallet,
    set_active,
    set_location,
    set_prices,
)


def test_a_new_kiosk_starts_registered_and_platform(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    assert kiosk.onboarding_stage == OnboardingStage.REGISTERED
    assert kiosk.kiosk_type == KioskType.PLATFORM


def test_a_new_kiosk_gets_a_paper_row(db_session):
    """Nothing downstream should have to guess whether one exists."""
    kiosk = create_kiosk(db_session, name="Library")
    db_session.flush()
    assert db_session.get(KioskPaper, kiosk.id) is not None


def test_a_new_kiosk_can_be_given_a_tray_size(db_session):
    kiosk = create_kiosk(db_session, name="Library", paper_capacity=500)
    db_session.flush()
    assert db_session.get(KioskPaper, kiosk.id).capacity == 500


def test_a_zero_capacity_tray_is_refused(db_session):
    with pytest.raises(BadRequest):
        create_kiosk(db_session, name="Library", paper_capacity=0)


def test_a_blank_name_is_refused(db_session):
    with pytest.raises(BadRequest):
        create_kiosk(db_session, name="   ")


def test_names_are_trimmed(db_session):
    assert create_kiosk(db_session, name="  Library  ").name == "Library"


def test_a_duplicate_name_is_refused(db_session):
    create_kiosk(db_session, name="Library")
    db_session.flush()
    with pytest.raises(Conflict):
        create_kiosk(db_session, name="Library")


def test_rename_refuses_a_name_another_kiosk_holds(db_session):
    create_kiosk(db_session, name="Library")
    other = create_kiosk(db_session, name="Canteen")
    db_session.flush()

    with pytest.raises(Conflict):
        rename_kiosk(db_session, other, "Library")


def test_rename_to_its_own_name_is_allowed(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    db_session.flush()
    assert rename_kiosk(db_session, kiosk, "Library").name == "Library"


def test_location_requires_both_coordinates(db_session):
    """A kiosk with one coordinate cannot be placed, and would silently vanish
    from any "near me" listing."""
    kiosk = create_kiosk(db_session, name="Library")
    with pytest.raises(BadRequest):
        set_location(db_session, kiosk, latitude=12.9)


def test_location_rejects_impossible_coordinates(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    with pytest.raises(BadRequest):
        set_location(db_session, kiosk, latitude=91.0, longitude=0.0)
    with pytest.raises(BadRequest):
        set_location(db_session, kiosk, latitude=0.0, longitude=181.0)


def test_location_accepts_a_description_alone(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    set_location(db_session, kiosk, description="Ground floor, by the stairs")
    assert kiosk.location_description == "Ground floor, by the stairs"


def test_changing_a_live_kiosk_to_sold_drops_it_out_of_live(db_session):
    """Its owner's Razorpay has not been verified for it, so leaving it selling
    would route students' money to an account that may not exist."""
    kiosk = create_kiosk(db_session, name="Library")
    kiosk.onboarding_stage = OnboardingStage.LIVE
    db_session.flush()

    change_type(db_session, kiosk, KioskType.SOLD)

    assert kiosk.kiosk_type == KioskType.SOLD
    assert kiosk.onboarding_stage == OnboardingStage.APPROVED
    assert "verified" in kiosk.onboarding_note


def test_changing_a_configured_kiosk_to_saas_also_drops_it_back(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    kiosk.onboarding_stage = OnboardingStage.CONFIGURED
    db_session.flush()

    change_type(db_session, kiosk, KioskType.SAAS)
    assert kiosk.onboarding_stage == OnboardingStage.APPROVED


def test_changing_to_platform_leaves_a_live_kiosk_live(db_session):
    """The platform's own keys always work, so nothing is left uncollected."""
    kiosk = create_kiosk(db_session, name="Library", kiosk_type=KioskType.SOLD)
    kiosk.onboarding_stage = OnboardingStage.LIVE
    db_session.flush()

    change_type(db_session, kiosk, KioskType.PLATFORM)
    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_changing_a_registered_kiosk_to_sold_does_not_move_its_stage(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    change_type(db_session, kiosk, KioskType.SOLD)
    assert kiosk.onboarding_stage == OnboardingStage.REGISTERED


def test_changing_to_the_same_type_is_a_no_op(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    kiosk.onboarding_stage = OnboardingStage.LIVE
    db_session.flush()

    change_type(db_session, kiosk, KioskType.PLATFORM)
    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_wallet_cannot_be_enabled_at_an_owner_gateway_kiosk(db_session):
    """Top-ups sit in Printvendo's account. Spending them where the owner
    collects means Printvendo keeps the cash and the owner prints for free."""
    kiosk = create_kiosk(db_session, name="Library", kiosk_type=KioskType.SOLD)
    with pytest.raises(BadRequest):
        set_accepts_wallet(db_session, kiosk, accepts_wallet=True)


def test_wallet_can_be_enabled_at_a_platform_kiosk(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    set_accepts_wallet(db_session, kiosk, accepts_wallet=True)
    assert kiosk.accepts_wallet is True


def test_wallet_can_always_be_turned_off(db_session):
    kiosk = create_kiosk(db_session, name="Library", kiosk_type=KioskType.SAAS)
    set_accepts_wallet(db_session, kiosk, accepts_wallet=False)
    assert kiosk.accepts_wallet is False


def test_deactivating_does_not_retire(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    kiosk.onboarding_stage = OnboardingStage.LIVE
    db_session.flush()

    set_active(db_session, kiosk, is_active=False)
    assert kiosk.is_active is False
    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_prices_are_stored_as_decimal(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    set_prices(db_session, kiosk, bw_single=Decimal("2.50"))
    db_session.flush()
    assert kiosk.price_bw_single == Decimal("2.50")


def test_a_negative_price_is_refused(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    with pytest.raises(BadRequest):
        set_prices(db_session, kiosk, bw_single=Decimal("-1"))


def test_setting_one_price_leaves_the_others_alone(db_session):
    kiosk = create_kiosk(db_session, name="Library")
    set_prices(db_session, kiosk, bw_single=Decimal("2"), color_single=Decimal("8"))
    set_prices(db_session, kiosk, bw_single=Decimal("3"))

    assert kiosk.price_bw_single == Decimal("3")
    assert kiosk.price_color_single == Decimal("8")
