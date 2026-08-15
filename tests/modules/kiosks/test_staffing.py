from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import BadRequest, Conflict
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.enums import AssignmentRole
from app.modules.kiosks.models import KioskAssignment, StaffInvite
from app.modules.kiosks.registry import create_kiosk
from app.modules.kiosks.staffing import (
    _hash,
    accept_invite,
    invite_staff,
    list_staff,
    revoke_invite,
    unassign,
)


@pytest.fixture
def kiosk(db_session):
    k = create_kiosk(db_session, name="Library")
    db_session.flush()
    return k


@pytest.fixture
def owner(db_session) -> User:
    u = User(email="owner@example.com", hashed_password="x")
    db_session.add(u)
    db_session.flush()
    return u


def _user(db_session, email: str) -> User:
    u = User(email=email, hashed_password="x")
    db_session.add(u)
    db_session.flush()
    return u


def _invite(db_session, kiosk, owner, email="refiller@example.com") -> str:
    token = invite_staff(
        db_session,
        kiosk,
        email=email,
        role=AssignmentRole.REFILLER,
        invited_by_user_id=owner.id,
    )
    db_session.flush()
    return token


def test_inviting_creates_no_assignment(db_session, kiosk, owner):
    """Nothing is bound until the invitee accepts."""
    _invite(db_session, kiosk, owner)
    assert db_session.query(KioskAssignment).count() == 0


def test_the_response_is_identical_for_a_known_and_unknown_address(
    db_session, kiosk, owner
):
    """This is the enumeration oracle the old backend had: an owner could tell
    which addresses had accounts, and which of those were refillers."""
    _user(db_session, "exists@example.com")

    known = _invite(db_session, kiosk, owner, "exists@example.com")
    unknown = _invite(db_session, kiosk, owner, "nobody@example.com")

    assert isinstance(known, str) and isinstance(unknown, str)
    assert len(known) == len(unknown)


def test_the_invite_is_stored_only_as_a_hash(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    assert db_session.query(StaffInvite).filter_by(token_hash=token).count() == 0
    assert db_session.query(StaffInvite).filter_by(token_hash=_hash(token)).count() == 1


def test_emails_are_normalised(db_session, kiosk, owner):
    _invite(db_session, kiosk, owner, "  Refiller@Example.COM ")
    assert db_session.query(StaffInvite).one().email == "refiller@example.com"


def test_a_malformed_address_is_refused(db_session, kiosk, owner):
    with pytest.raises(BadRequest):
        _invite(db_session, kiosk, owner, "not-an-address")


def test_accepting_binds_the_invitee(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")

    accepted_kiosk, role = accept_invite(db_session, token, user=refiller)
    db_session.flush()

    assert accepted_kiosk.id == kiosk.id
    assert role is AssignmentRole.REFILLER

    binding = (
        db_session.query(KioskAssignment)
        .filter_by(kiosk_id=kiosk.id, user_id=refiller.id)
        .one()
    )
    assert binding.role == AssignmentRole.REFILLER


def test_accepting_grants_the_platform_role(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")

    accept_invite(db_session, token, user=refiller)
    db_session.flush()

    assert Role.REFILLER in identity_repo.roles_of(db_session, refiller.id)


def test_someone_else_cannot_redeem_a_forwarded_link(db_session, kiosk, owner):
    """Otherwise forwarding the email attaches an arbitrary account to the
    kiosk."""
    token = _invite(db_session, kiosk, owner, "intended@example.com")
    interloper = _user(db_session, "interloper@example.com")

    with pytest.raises(BadRequest):
        accept_invite(db_session, token, user=interloper)


def test_the_wrong_recipient_gets_the_same_message_as_a_bad_token(
    db_session, kiosk, owner
):
    """Saying "this link is not for you" discloses that someone else was
    invited."""
    token = _invite(db_session, kiosk, owner, "intended@example.com")
    interloper = _user(db_session, "interloper@example.com")

    with pytest.raises(BadRequest) as wrong_person:
        accept_invite(db_session, token, user=interloper)
    with pytest.raises(BadRequest) as bad_token:
        accept_invite(db_session, "never-issued", user=interloper)

    assert str(wrong_person.value) == str(bad_token.value)


def test_an_invite_cannot_be_used_twice(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")

    accept_invite(db_session, token, user=refiller)
    db_session.flush()

    with pytest.raises(BadRequest):
        accept_invite(db_session, token, user=refiller)


def test_an_expired_invite_is_refused(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")

    invite = db_session.query(StaffInvite).one()
    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(BadRequest):
        accept_invite(db_session, token, user=refiller)


def test_a_revoked_invite_is_refused(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")

    revoke_invite(
        db_session, kiosk, email="refiller@example.com", role=AssignmentRole.REFILLER
    )
    db_session.flush()

    with pytest.raises(BadRequest):
        accept_invite(db_session, token, user=refiller)


def test_reinviting_supersedes_the_earlier_link(db_session, kiosk, owner):
    """Pressing invite twice must not leave two live links for one address."""
    first = _invite(db_session, kiosk, owner)
    second = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")

    with pytest.raises(BadRequest):
        accept_invite(db_session, first, user=refiller)

    assert accept_invite(db_session, second, user=refiller) is not None


def test_inviting_someone_who_already_works_there_is_refused(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")
    accept_invite(db_session, token, user=refiller)
    db_session.flush()

    with pytest.raises(Conflict):
        _invite(db_session, kiosk, owner)


def test_list_staff_shows_accepted_people_only(db_session, kiosk, owner):
    _invite(db_session, kiosk, owner, "pending@example.com")
    token = _invite(db_session, kiosk, owner, "accepted@example.com")
    refiller = _user(db_session, "accepted@example.com")
    accept_invite(db_session, token, user=refiller)
    db_session.flush()

    staff = list_staff(db_session, kiosk)
    assert [u.email for u, _ in staff] == ["accepted@example.com"]


def test_unassign_removes_the_binding(db_session, kiosk, owner):
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")
    accept_invite(db_session, token, user=refiller)
    db_session.flush()

    unassign(db_session, kiosk, user_id=refiller.id, role=AssignmentRole.REFILLER)
    db_session.flush()

    assert list_staff(db_session, kiosk) == []


def test_unassign_keeps_the_platform_role(db_session, kiosk, owner):
    """They may still refill at another shop; dropping the role would sign them
    out of that one."""
    token = _invite(db_session, kiosk, owner)
    refiller = _user(db_session, "refiller@example.com")
    accept_invite(db_session, token, user=refiller)
    db_session.flush()

    unassign(db_session, kiosk, user_id=refiller.id, role=AssignmentRole.REFILLER)
    db_session.flush()

    assert Role.REFILLER in identity_repo.roles_of(db_session, refiller.id)


def test_an_owner_cannot_bind_another_shops_refiller_without_consent(
    db_session, kiosk, owner
):
    """The harvesting hole: in the old backend this owner could attach a
    competitor's refiller and then read their name and email."""
    other_kiosk = create_kiosk(db_session, name="Rival Shop")
    other_owner = _user(db_session, "rival@example.com")
    db_session.flush()

    their_refiller = _user(db_session, "their-refiller@example.com")
    token = invite_staff(
        db_session,
        other_kiosk,
        email="their-refiller@example.com",
        role=AssignmentRole.REFILLER,
        invited_by_user_id=other_owner.id,
    )
    db_session.flush()
    accept_invite(db_session, token, user=their_refiller)
    db_session.flush()

    # This owner can invite the address, but that binds nothing on its own.
    _invite(db_session, kiosk, owner, "their-refiller@example.com")
    assert list_staff(db_session, kiosk) == []
