"""Asking the machine in a shop to do something, and hearing back."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import BadRequest, NotFound
from app.modules.kiosks.commands import (
    COMMAND_LIFETIME,
    claim_commands,
    recent_commands,
    report_command,
    report_recovered,
    report_stuck,
    request_command,
)
from app.modules.kiosks.enums import (
    DeviceCommandKind,
    DeviceCommandState,
    OnboardingStage,
)
from app.modules.kiosks.models import Kiosk, KioskDevice
from app.modules.kiosks.onboarding import PlatformOnlyBilling

RESTART_AGENT = DeviceCommandKind.RESTART_AGENT
RESTART_PRINTING = DeviceCommandKind.RESTART_PRINTING

BILLING = PlatformOnlyBilling()


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(name="Command Test Shop", onboarding_stage=OnboardingStage.LIVE)
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def device(db_session, kiosk) -> KioskDevice:
    device = KioskDevice(kiosk_id=kiosk.id, device_key="cmd-test", token_hash="x" * 64)
    db_session.add(device)
    db_session.flush()
    return device


# ── asking ──────────────────────────────────────────────────────────────────


def test_a_command_starts_queued(db_session, kiosk, device):
    command = request_command(db_session, kiosk, RESTART_AGENT)

    assert command.state is DeviceCommandState.QUEUED
    assert command.public_id.startswith("cmd_")


def test_a_kiosk_with_no_machine_cannot_be_asked(db_session, kiosk):
    with pytest.raises(BadRequest):
        request_command(db_session, kiosk, RESTART_AGENT)


def test_asking_twice_does_not_queue_twice(db_session, kiosk, device):
    """A button that appears to do nothing gets pressed again. That must not
    queue two restarts to run back to back."""
    first = request_command(db_session, kiosk, RESTART_AGENT)
    second = request_command(db_session, kiosk, RESTART_AGENT)

    assert second.id == first.id


def test_a_different_kind_is_a_different_command(db_session, kiosk, device):
    first = request_command(db_session, kiosk, RESTART_AGENT)
    second = request_command(db_session, kiosk, RESTART_PRINTING)

    assert second.id != first.id


def test_asking_again_after_the_first_expired_queues_a_new_one(db_session, kiosk, device):
    long_ago = datetime.now(UTC) - COMMAND_LIFETIME - timedelta(minutes=1)
    first = request_command(db_session, kiosk, RESTART_AGENT, now=long_ago)

    second = request_command(db_session, kiosk, RESTART_AGENT)

    assert second.id != first.id


# ── claiming ────────────────────────────────────────────────────────────────


def test_claiming_hands_over_everything_waiting(db_session, kiosk, device):
    """Not one per pass. The first restart kills the loop that would have
    fetched the second."""
    request_command(db_session, kiosk, RESTART_AGENT)
    request_command(db_session, kiosk, RESTART_PRINTING)

    claimed = claim_commands(db_session, device)

    assert {c.kind for c in claimed} == {RESTART_AGENT, RESTART_PRINTING}
    assert all(c.state is DeviceCommandState.SENT for c in claimed)


def test_a_claimed_command_is_not_handed_out_again(db_session, kiosk, device):
    request_command(db_session, kiosk, RESTART_AGENT)
    claim_commands(db_session, device)

    assert claim_commands(db_session, device) == []


def test_a_stale_command_expires_rather_than_running(db_session, kiosk, device):
    long_ago = datetime.now(UTC) - COMMAND_LIFETIME - timedelta(minutes=1)
    command = request_command(db_session, kiosk, RESTART_AGENT, now=long_ago)

    assert claim_commands(db_session, device) == []
    assert command.state is DeviceCommandState.EXPIRED


def test_a_machine_never_sees_another_kiosks_command(db_session, kiosk, device):
    other = Kiosk(name="Somebody Else", onboarding_stage=OnboardingStage.LIVE)
    db_session.add(other)
    db_session.flush()
    db_session.add(
        KioskDevice(kiosk_id=other.id, device_key="other", token_hash="y" * 64)
    )
    db_session.flush()
    request_command(db_session, other, RESTART_AGENT)

    assert claim_commands(db_session, device) == []


# ── reporting ───────────────────────────────────────────────────────────────


def test_a_command_can_succeed(db_session, kiosk, device):
    command = request_command(db_session, kiosk, RESTART_PRINTING)
    claim_commands(db_session, device)

    reported = report_command(db_session, device, command.public_id, succeeded=True)

    assert reported.state is DeviceCommandState.SUCCEEDED
    assert reported.finished_at is not None


def test_a_failure_keeps_the_reason(db_session, kiosk, device):
    command = request_command(db_session, kiosk, RESTART_PRINTING)
    claim_commands(db_session, device)

    reported = report_command(
        db_session,
        device,
        command.public_id,
        succeeded=False,
        error_message="cups is not installed",
    )

    assert reported.state is DeviceCommandState.FAILED
    assert reported.error_message == "cups is not installed"


def test_a_success_carries_no_error_text(db_session, kiosk, device):
    command = request_command(db_session, kiosk, RESTART_PRINTING)
    claim_commands(db_session, device)

    reported = report_command(
        db_session, device, command.public_id, succeeded=True, error_message="ignore me"
    )

    assert reported.error_message is None


def test_another_kiosks_command_is_not_found(db_session, kiosk, device):
    other = Kiosk(name="Somebody Else", onboarding_stage=OnboardingStage.LIVE)
    db_session.add(other)
    db_session.flush()
    db_session.add(
        KioskDevice(kiosk_id=other.id, device_key="other", token_hash="y" * 64)
    )
    db_session.flush()
    theirs = request_command(db_session, other, RESTART_AGENT)

    with pytest.raises(NotFound):
        report_command(db_session, device, theirs.public_id, succeeded=True)


def test_a_finished_command_cannot_be_reported_again(db_session, kiosk, device):
    command = request_command(db_session, kiosk, RESTART_AGENT)
    claim_commands(db_session, device)
    report_command(db_session, device, command.public_id, succeeded=True)

    with pytest.raises(BadRequest):
        report_command(db_session, device, command.public_id, succeeded=False)


def test_recent_commands_are_newest_first(db_session, kiosk, device):
    first = request_command(db_session, kiosk, RESTART_AGENT)
    second = request_command(db_session, kiosk, RESTART_PRINTING)

    assert [c.id for c in recent_commands(db_session, kiosk)] == [second.id, first.id]


# ── a stuck printer closes the shop ─────────────────────────────────────────


def test_a_stuck_printer_closes_the_shop(db_session, kiosk, device):
    closed = report_stuck(db_session, device, kiosk, billing=BILLING)

    assert closed is True
    assert kiosk.onboarding_stage is OnboardingStage.MAINTENANCE
    assert device.stuck_since is not None


def test_saying_it_again_does_not_close_it_again(db_session, kiosk, device):
    report_stuck(db_session, device, kiosk, billing=BILLING)

    assert report_stuck(db_session, device, kiosk, billing=BILLING) is False


def test_recovering_reopens_the_shop(db_session, kiosk, device):
    report_stuck(db_session, device, kiosk, billing=BILLING)

    reopened = report_recovered(db_session, device, kiosk, billing=BILLING)

    assert reopened is True
    assert kiosk.onboarding_stage is OnboardingStage.LIVE
    assert device.stuck_since is None
    assert kiosk.onboarding_note is None


def test_a_shop_a_person_closed_is_not_reopened_by_a_printer(db_session, kiosk, device):
    """An owner changing a toner cartridge must not have the shop reopened
    under them because a queue happened to clear."""
    kiosk.onboarding_stage = OnboardingStage.MAINTENANCE
    db_session.flush()

    assert report_recovered(db_session, device, kiosk, billing=BILLING) is False
    assert kiosk.onboarding_stage is OnboardingStage.MAINTENANCE


def test_a_kiosk_that_is_not_live_is_not_moved(db_session, kiosk, device):
    kiosk.onboarding_stage = OnboardingStage.SUSPENDED_BILLING
    db_session.flush()

    assert report_stuck(db_session, device, kiosk, billing=BILLING) is False
    assert kiosk.onboarding_stage is OnboardingStage.SUSPENDED_BILLING


def test_a_jam_while_a_person_has_the_shop_closed_does_not_hand_it_back(
    db_session, kiosk, device
):
    """The sequence the earlier test misses, and the whole reason `stuck_since`
    exists.

    An owner puts the shop into maintenance to change a cartridge. A job that
    was already queued jams, so the machine reports stuck -- and we do *not*
    close the shop, because it is already closed and not by us. `stuck_since`
    must therefore stay empty: it is the record of **our** decision, and
    setting it here made the next recovery reopen a shop with the printer in
    pieces on the counter.
    """
    kiosk.onboarding_stage = OnboardingStage.MAINTENANCE
    db_session.flush()

    assert report_stuck(db_session, device, kiosk, billing=BILLING) is False
    assert device.stuck_since is None

    # The jam clears. Nothing here is ours to reopen.
    assert report_recovered(db_session, device, kiosk, billing=BILLING) is False
    assert kiosk.onboarding_stage is OnboardingStage.MAINTENANCE


def test_a_jam_at_a_suspended_shop_records_nothing_either(db_session, kiosk, device):
    """Same rule, and it is the rule rather than a special case for
    maintenance: a shop with a billing problem is not one a paper jam closed."""
    kiosk.onboarding_stage = OnboardingStage.SUSPENDED_BILLING
    db_session.flush()

    assert report_stuck(db_session, device, kiosk, billing=BILLING) is False

    assert device.stuck_since is None
