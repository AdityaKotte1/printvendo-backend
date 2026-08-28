"""How a Raspberry Pi proves it is the machine at a particular kiosk."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import BadRequest, Conflict, Unauthorized
from app.modules.kiosks.devices import (
    ENROLMENT_LIFETIME,
    HEARTBEAT_WINDOW,
    authenticate_device,
    device_of,
    is_online,
    issue_enrolment_code,
    record_heartbeat,
    register_device,
    revoke_device,
    rotate_token,
)
from app.modules.kiosks.enums import DeviceStatus, OnboardingStage
from app.modules.kiosks.models import DeviceEnrolment, Kiosk, KioskDevice


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(name="Device Test Shop", onboarding_stage=OnboardingStage.LIVE)
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def other_kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(name="Another Shop", onboarding_stage=OnboardingStage.LIVE)
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


def enrol(db, kiosk) -> str:
    return issue_enrolment_code(db, kiosk, created_by_user_id=None).code


# ── enrolment ───────────────────────────────────────────────────────────────


def test_an_enrolment_code_is_returned_once_and_stored_only_hashed(db_session, kiosk):
    """A database dump must not yield a code that can attach a machine."""
    code = enrol(db_session, kiosk)
    db_session.flush()

    rows = db_session.query(DeviceEnrolment).all()
    assert len(rows) == 1
    assert code not in rows[0].code_hash
    assert rows[0].code_hash != code


def test_registering_with_a_code_gives_the_device_a_token(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk), agent_version="2.0.0")

    assert issued.token
    assert issued.device.kiosk_id == kiosk.id
    assert issued.device.agent_version == "2.0.0"


def test_a_machine_enrolled_elsewhere_is_refused_with_the_shop_that_holds_it(
    db_session, kiosk, other_kiosk
):
    """`device_key` is unique across the estate and the agent derives it from
    the machine (hostname and architecture), so it is the same string every time
    that box enrols.

    An operator who had assigned Pis to the wrong shops tried to move one, and
    both branches below assign device_key -- so the insert hit the unique
    constraint and the installer got a bare 500 saying "Something went wrong.
    Please try again", at a shop counter, with no way to learn that the machine
    was still attached somewhere else. Retrying could never work.

    Refused rather than moved: two different machines can share a hostname, and
    silently detaching one shop's Pi because another box calls itself
    `raspberrypi` would close a working kiosk.
    """
    register_device(db_session, enrol(db_session, other_kiosk), device_key="pi-1-aarch64")
    db_session.flush()

    with pytest.raises(Conflict) as refused:
        register_device(db_session, enrol(db_session, kiosk), device_key="pi-1-aarch64")

    # Names the shop to go and detach it from, or the operator is still stuck.
    assert other_kiosk.name in refused.value.detail


def test_the_same_machine_re_enrolling_at_its_own_kiosk_is_fine(db_session, kiosk):
    """Re-enrolling in place is ordinary -- a token rotation, not a move."""
    first = register_device(db_session, enrol(db_session, kiosk), device_key="pi-1-aarch64")
    db_session.flush()

    second = register_device(db_session, enrol(db_session, kiosk), device_key="pi-1-aarch64")

    assert second.device.id == first.device.id
    assert second.token != first.token


def test_the_device_token_is_stored_only_as_a_hash(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk))
    db_session.flush()

    stored = db_session.get(KioskDevice, issued.device.id)
    assert issued.token not in stored.token_hash


def test_a_code_can_only_be_spent_once(db_session, kiosk):
    code = enrol(db_session, kiosk)
    register_device(db_session, code)

    with pytest.raises(BadRequest):
        register_device(db_session, code)


def test_an_expired_code_is_refused(db_session, kiosk):
    code = enrol(db_session, kiosk)
    db_session.flush()
    row = db_session.query(DeviceEnrolment).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(BadRequest):
        register_device(db_session, code)


def test_an_unknown_code_and_a_spent_code_give_the_same_message(db_session, kiosk):
    code = enrol(db_session, kiosk)
    register_device(db_session, code)

    with pytest.raises(BadRequest) as spent:
        register_device(db_session, code)
    with pytest.raises(BadRequest) as unknown:
        register_device(db_session, "dve_nosuchcodeatall")

    assert str(spent.value) == str(unknown.value)


def test_the_code_lifetime_is_short_enough_to_matter():
    """It is typed into a terminal during an install, not mailed to anyone."""
    assert ENROLMENT_LIFETIME <= timedelta(hours=24)


# ── one device per kiosk, and re-enrolment kills the old token ──────────────


def test_re_enrolling_a_kiosk_replaces_the_device_and_kills_the_old_token(
    db_session, kiosk
):
    """Swapping a Pi must not leave the old one able to pull print jobs -- that
    machine may have been sold, returned, or stolen."""
    first = register_device(db_session, enrol(db_session, kiosk))
    second = register_device(db_session, enrol(db_session, kiosk))
    db_session.flush()

    assert authenticate_device(db_session, second.token).kiosk_id == kiosk.id
    with pytest.raises(Unauthorized):
        authenticate_device(db_session, first.token)


def test_a_kiosk_has_at_most_one_device(db_session, kiosk):
    register_device(db_session, enrol(db_session, kiosk))
    register_device(db_session, enrol(db_session, kiosk))
    db_session.flush()

    assert db_session.query(KioskDevice).filter_by(kiosk_id=kiosk.id).count() == 1


def test_the_device_key_survives_re_registration(db_session, kiosk):
    """The key is what the agent writes into its config and what an operator
    reads back over SSH. Changing it on every re-register would make the field
    useless for identifying which physical box is which."""
    first = register_device(db_session, enrol(db_session, kiosk))
    key = first.device.device_key

    second = register_device(
        db_session, enrol(db_session, kiosk), device_key=key
    )

    assert second.device.device_key == key


# ── authentication ──────────────────────────────────────────────────────────


def test_a_valid_token_resolves_to_its_own_kiosk(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk))
    db_session.flush()

    assert authenticate_device(db_session, issued.token).kiosk_id == kiosk.id


def test_a_token_is_bound_to_exactly_one_kiosk(db_session, kiosk, other_kiosk):
    """The old backend's device token was checked but its scope was not, so one
    kiosk's Pi could fetch another's job file."""
    mine = register_device(db_session, enrol(db_session, kiosk))
    theirs = register_device(db_session, enrol(db_session, other_kiosk))
    db_session.flush()

    assert authenticate_device(db_session, mine.token).kiosk_id == kiosk.id
    assert authenticate_device(db_session, theirs.token).kiosk_id == other_kiosk.id


@pytest.mark.parametrize(
    "rubbish",
    [
        # None is the ordinary case, not an exotic one: it is what a missing
        # X-Device-Token header produces. Hashing it would raise, and the caller
        # would get a 500 where it should get a 401.
        None,
        "",
        "   ",
        "not-a-token",
        "dvt_" + "a" * 43,
    ],
)
def test_a_token_that_is_not_ours_is_refused(db_session, kiosk, rubbish):
    register_device(db_session, enrol(db_session, kiosk))
    db_session.flush()

    with pytest.raises(Unauthorized):
        authenticate_device(db_session, rubbish)


def test_a_retired_kiosks_device_cannot_authenticate(db_session, kiosk):
    """A retired kiosk is out of service; its Pi must stop being able to pull
    work even if the box is still plugged in somewhere."""
    issued = register_device(db_session, enrol(db_session, kiosk))
    kiosk.onboarding_stage = OnboardingStage.RETIRED
    db_session.flush()

    with pytest.raises(Unauthorized):
        authenticate_device(db_session, issued.token)


def test_revoking_a_device_ends_its_access_immediately(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk))
    db_session.flush()

    revoke_device(db_session, kiosk)
    db_session.flush()

    with pytest.raises(Unauthorized):
        authenticate_device(db_session, issued.token)
    assert device_of(db_session, kiosk) is None


def test_rotating_the_token_invalidates_the_previous_one(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk))
    db_session.flush()

    fresh = rotate_token(db_session, issued.device)
    db_session.flush()

    assert authenticate_device(db_session, fresh).kiosk_id == kiosk.id
    with pytest.raises(Unauthorized):
        authenticate_device(db_session, issued.token)


# ── heartbeat ───────────────────────────────────────────────────────────────


def test_a_heartbeat_records_when_and_what_version(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk))

    record_heartbeat(db_session, issued.device, agent_version="2.1.0")

    assert issued.device.agent_version == "2.1.0"
    assert issued.device.last_heartbeat_at is not None
    assert issued.device.status is DeviceStatus.ONLINE


def test_a_device_that_has_never_reported_is_offline(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk))

    assert is_online(issued.device) is False


def test_a_device_that_reported_just_now_is_online(db_session, kiosk):
    issued = register_device(db_session, enrol(db_session, kiosk))
    record_heartbeat(db_session, issued.device)

    assert is_online(issued.device) is True


def test_a_device_that_went_quiet_is_offline_without_anyone_telling_us(
    db_session, kiosk
):
    """Online is derived from the last heartbeat, not from a status column
    somebody has to remember to update. A Pi whose power was pulled cannot send
    "I am going offline", and the old backend's status stayed ONLINE until a
    human noticed."""
    issued = register_device(db_session, enrol(db_session, kiosk))
    long_ago = datetime.now(UTC) - HEARTBEAT_WINDOW - timedelta(seconds=1)
    record_heartbeat(db_session, issued.device, now=long_ago)

    assert issued.device.status is DeviceStatus.ONLINE  # what it last claimed
    assert is_online(issued.device) is False  # what is actually true


def test_the_issued_code_reports_its_own_expiry(db_session, kiosk):
    """The installer has to be told how long they have, and an expiry worked out
    again at the call site is one that will eventually disagree with the row."""
    issued = issue_enrolment_code(db_session, kiosk, created_by_user_id=None)
    db_session.flush()

    row = db_session.query(DeviceEnrolment).one()
    assert issued.expires_at == row.expires_at
