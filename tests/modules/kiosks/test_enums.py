import pytest

from app.modules.kiosks.enums import (
    TRANSITIONS,
    AssignmentRole,
    DeviceStatus,
    KioskType,
    OnboardingStage,
    can_transition,
)


def test_kiosk_types_are_the_three_business_relationships():
    assert {t.value for t in KioskType} == {"platform", "sold", "saas"}


def test_assignment_roles_are_owner_and_refiller():
    assert {r.value for r in AssignmentRole} == {"owner", "refiller"}


def test_device_statuses_cover_the_reportable_states():
    assert {s.value for s in DeviceStatus} == {"offline", "online", "printing", "error"}


def test_every_stage_appears_in_the_transition_table():
    assert set(TRANSITIONS) == set(OnboardingStage)


def test_a_new_kiosk_starts_registered():
    assert OnboardingStage.REGISTERED.value == "registered"


def test_the_happy_path_is_permitted():
    assert can_transition(OnboardingStage.REGISTERED, OnboardingStage.APPROVED)
    assert can_transition(OnboardingStage.APPROVED, OnboardingStage.CONFIGURED)
    assert can_transition(OnboardingStage.CONFIGURED, OnboardingStage.LIVE)


def test_a_kiosk_cannot_skip_straight_to_live():
    """Skipping CONFIGURED is how an owned kiosk starts selling before its
    owner's Razorpay account is connected."""
    assert not can_transition(OnboardingStage.REGISTERED, OnboardingStage.LIVE)
    assert not can_transition(OnboardingStage.APPROVED, OnboardingStage.LIVE)


def test_live_and_maintenance_are_reversible():
    assert can_transition(OnboardingStage.LIVE, OnboardingStage.MAINTENANCE)
    assert can_transition(OnboardingStage.MAINTENANCE, OnboardingStage.LIVE)


def test_billing_suspension_is_reversible():
    assert can_transition(OnboardingStage.LIVE, OnboardingStage.SUSPENDED_BILLING)
    assert can_transition(OnboardingStage.SUSPENDED_BILLING, OnboardingStage.LIVE)


def test_retired_is_final():
    for stage in OnboardingStage:
        assert not can_transition(OnboardingStage.RETIRED, stage)


def test_every_stage_can_be_retired_except_retired_itself():
    for stage in OnboardingStage:
        if stage is OnboardingStage.RETIRED:
            continue
        assert can_transition(stage, OnboardingStage.RETIRED), stage


def test_a_stage_cannot_transition_to_itself():
    for stage in OnboardingStage:
        assert not can_transition(stage, stage)


@pytest.mark.parametrize("stage", list(OnboardingStage))
def test_every_stage_is_reachable_or_is_the_start(stage):
    """An unreachable stage is dead configuration that will mislead someone."""
    if stage is OnboardingStage.REGISTERED:
        return
    assert any(stage in targets for targets in TRANSITIONS.values()), stage
