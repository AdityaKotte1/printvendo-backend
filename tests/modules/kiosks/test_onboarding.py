import pytest

from app.core.errors import BadRequest, Conflict
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk
from app.modules.kiosks.onboarding import (
    PlatformOnlyBilling,
    is_selling,
    move_to,
    reconcile_billing_state,
)
from app.modules.kiosks.registry import create_kiosk


class Billing:
    """Controllable stand-in for the payments module."""

    def __init__(self, *, can_collect: bool) -> None:
        self.can_collect = can_collect

    def owner_can_collect(self, db, kiosk: Kiosk) -> bool:
        return self.can_collect


CONNECTED = Billing(can_collect=True)
NOT_CONNECTED = Billing(can_collect=False)


def _at(db_session, stage: OnboardingStage, kiosk_type=KioskType.PLATFORM) -> Kiosk:
    kiosk = create_kiosk(db_session, name=f"Shop {stage.value}", kiosk_type=kiosk_type)
    kiosk.onboarding_stage = stage
    db_session.flush()
    return kiosk


def test_the_happy_path_walks_forward(db_session):
    kiosk = _at(db_session, OnboardingStage.REGISTERED)

    move_to(db_session, kiosk, OnboardingStage.APPROVED, billing=CONNECTED)
    move_to(db_session, kiosk, OnboardingStage.CONFIGURED, billing=CONNECTED)
    move_to(db_session, kiosk, OnboardingStage.LIVE, billing=CONNECTED)

    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_an_undefined_transition_is_refused(db_session):
    kiosk = _at(db_session, OnboardingStage.REGISTERED)
    with pytest.raises(Conflict):
        move_to(db_session, kiosk, OnboardingStage.LIVE, billing=CONNECTED)


def test_the_refusal_names_both_stages(db_session):
    kiosk = _at(db_session, OnboardingStage.REGISTERED)
    with pytest.raises(Conflict) as caught:
        move_to(db_session, kiosk, OnboardingStage.LIVE, billing=CONNECTED)
    assert "registered" in str(caught.value)
    assert "live" in str(caught.value)


def test_moving_to_the_current_stage_is_a_no_op(db_session):
    kiosk = _at(db_session, OnboardingStage.LIVE)
    move_to(db_session, kiosk, OnboardingStage.LIVE, billing=NOT_CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_a_platform_kiosk_goes_live_without_owner_billing(db_session):
    """Our own keys always work."""
    kiosk = _at(db_session, OnboardingStage.CONFIGURED)
    move_to(db_session, kiosk, OnboardingStage.LIVE, billing=NOT_CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_a_sold_kiosk_cannot_go_live_without_owner_billing(db_session):
    """The gate that protects the owner: money collected before their Razorpay
    is connected has nowhere correct to go."""
    kiosk = _at(db_session, OnboardingStage.CONFIGURED, KioskType.SOLD)
    with pytest.raises(BadRequest) as caught:
        move_to(db_session, kiosk, OnboardingStage.LIVE, billing=NOT_CONNECTED)
    assert "Razorpay" in str(caught.value)


def test_a_saas_kiosk_cannot_go_live_without_owner_billing(db_session):
    kiosk = _at(db_session, OnboardingStage.CONFIGURED, KioskType.SAAS)
    with pytest.raises(BadRequest):
        move_to(db_session, kiosk, OnboardingStage.LIVE, billing=NOT_CONNECTED)


def test_a_sold_kiosk_goes_live_once_billing_is_connected(db_session):
    kiosk = _at(db_session, OnboardingStage.CONFIGURED, KioskType.SOLD)
    move_to(db_session, kiosk, OnboardingStage.LIVE, billing=CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_the_default_billing_check_fails_closed(db_session):
    """A stub that answered True would let a kiosk sell with no verified route
    to its owner -- the exact thing the gate prevents."""
    kiosk = _at(db_session, OnboardingStage.CONFIGURED, KioskType.SOLD)
    with pytest.raises(BadRequest):
        move_to(db_session, kiosk, OnboardingStage.LIVE, billing=PlatformOnlyBilling())


def test_a_lapse_suspends_a_live_owned_kiosk(db_session):
    """The old backend only guarded the promotion, so a kiosk whose owner let
    their subscription lapse kept selling."""
    kiosk = _at(db_session, OnboardingStage.LIVE, KioskType.SOLD)
    reconcile_billing_state(db_session, kiosk, billing=NOT_CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.SUSPENDED_BILLING


def test_a_lapse_also_suspends_a_kiosk_in_maintenance(db_session):
    kiosk = _at(db_session, OnboardingStage.MAINTENANCE, KioskType.SOLD)
    reconcile_billing_state(db_session, kiosk, billing=NOT_CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.SUSPENDED_BILLING


def test_fixing_billing_restores_a_suspended_kiosk(db_session):
    kiosk = _at(db_session, OnboardingStage.SUSPENDED_BILLING, KioskType.SOLD)
    reconcile_billing_state(db_session, kiosk, billing=CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.LIVE
    assert kiosk.onboarding_note is None


def test_the_suspension_note_explains_itself(db_session):
    kiosk = _at(db_session, OnboardingStage.LIVE, KioskType.SOLD)
    reconcile_billing_state(db_session, kiosk, billing=NOT_CONNECTED)
    assert "collect payments" in kiosk.onboarding_note


def test_a_platform_kiosk_is_never_suspended_for_billing(db_session):
    kiosk = _at(db_session, OnboardingStage.LIVE)
    reconcile_billing_state(db_session, kiosk, billing=NOT_CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.LIVE


def test_reconcile_does_not_promote_a_kiosk_that_never_went_live(db_session):
    """Fixing billing must not skip approval and configuration."""
    kiosk = _at(db_session, OnboardingStage.REGISTERED, KioskType.SOLD)
    reconcile_billing_state(db_session, kiosk, billing=CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.REGISTERED


def test_reconcile_leaves_a_retired_kiosk_alone(db_session):
    kiosk = _at(db_session, OnboardingStage.RETIRED, KioskType.SOLD)
    reconcile_billing_state(db_session, kiosk, billing=NOT_CONNECTED)
    assert kiosk.onboarding_stage == OnboardingStage.RETIRED


def test_only_a_live_active_kiosk_is_selling(db_session):
    kiosk = _at(db_session, OnboardingStage.LIVE)
    assert is_selling(kiosk) is True


def test_maintenance_is_not_selling(db_session):
    """Maintenance exists so an owner can work on a machine without the shop
    looking broken."""
    kiosk = _at(db_session, OnboardingStage.MAINTENANCE)
    assert is_selling(kiosk) is False


def test_a_suspended_kiosk_is_not_selling(db_session):
    kiosk = _at(db_session, OnboardingStage.SUSPENDED_BILLING)
    assert is_selling(kiosk) is False


def test_a_deactivated_live_kiosk_is_not_selling(db_session):
    kiosk = _at(db_session, OnboardingStage.LIVE)
    kiosk.is_active = False
    assert is_selling(kiosk) is False
