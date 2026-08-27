"""The first admin, and why this is not a general way to make admins.

Roles are granted through an audited admin route, which requires an admin. On an
empty database nobody can make the first one, so this exists. It is deliberately
awkward: it refuses when an admin already exists, and it writes to the same
audit trail the route does, because an admin created out of band and invisibly
is exactly the hole the audit trail is supposed to close.
"""

import pytest

from app.cli.bootstrap import ALREADY_HAVE_ONE, bootstrap_admin
from app.core.errors import BadRequest, Conflict
from app.modules.identity import Role
from app.modules.identity import repository as identity_repo
from app.modules.ops import entries_for

PASSWORD = "correct horse battery staple"


def test_the_first_admin_can_be_made(db_session):
    admin = bootstrap_admin(db_session, email="ops@example.com", password=PASSWORD)

    assert Role.ADMIN in identity_repo.roles_of(db_session, admin.id)


def test_the_first_admin_is_also_a_student(db_session):
    """Every account starts as one; nothing about being an admin removes it."""
    admin = bootstrap_admin(db_session, email="ops@example.com", password=PASSWORD)

    assert Role.STUDENT in identity_repo.roles_of(db_session, admin.id)


def test_a_second_admin_is_refused(db_session):
    """Not a convenience. Once one admin exists there is an audited route, and
    a command line that keeps working is a way to grant admin without it."""
    bootstrap_admin(db_session, email="first@example.com", password=PASSWORD)

    with pytest.raises(Conflict) as refused:
        bootstrap_admin(db_session, email="second@example.com", password=PASSWORD)

    assert ALREADY_HAVE_ONE in str(refused.value)


def test_a_second_admin_is_possible_when_it_is_meant(db_session):
    """Recovery: the one admin left the company, or lost their password."""
    bootstrap_admin(db_session, email="first@example.com", password=PASSWORD)

    second = bootstrap_admin(
        db_session, email="second@example.com", password=PASSWORD, force=True
    )

    assert Role.ADMIN in identity_repo.roles_of(db_session, second.id)


def test_making_an_admin_this_way_is_still_in_the_audit_trail(db_session):
    """With no actor, because there was nobody to be one.

    An admin who appears in the database with no trace is indistinguishable from
    an intruder who granted themselves the role.
    """
    admin = bootstrap_admin(db_session, email="ops@example.com", password=PASSWORD)

    # The same action name the admin route records, not a second one for the
    # same event: "how did this account become an admin" must have one answer
    # to grep for.
    trail = entries_for(db_session, entity_type="user", entity_id=admin.public_id)
    assert [entry.action for entry in trail] == ["identity.role.granted"]
    assert trail[0].actor_user_id is None


def test_an_existing_account_can_be_promoted_rather_than_duplicated(db_session):
    """The operator already registered through the app and then found they had
    no way in. Refusing here would leave them creating a second account."""
    from app.modules.identity import register

    person = register(db_session, "ops@example.com", PASSWORD, "Ops")

    promoted = bootstrap_admin(db_session, email="ops@example.com", password=None)

    assert promoted.id == person.id
    assert Role.ADMIN in identity_repo.roles_of(db_session, promoted.id)


def test_promoting_an_existing_account_does_not_change_their_password(db_session):
    from app.core.security import verify_password
    from app.modules.identity import register

    register(db_session, "ops@example.com", PASSWORD, "Ops")

    promoted = bootstrap_admin(db_session, email="ops@example.com", password=None)

    assert verify_password(PASSWORD, promoted.hashed_password)


def test_a_new_account_needs_a_password(db_session):
    with pytest.raises(Conflict):
        bootstrap_admin(db_session, email="nobody@example.com", password=None)


# ── an admin who cannot sign in is not an admin ─────────────────────────────


def test_an_address_that_cannot_sign_in_is_refused(db_session):
    """The CLI took any string. `POST /v1/app/auth/login` validates with
    `EmailStr`, which rejects reserved TLDs -- so `admin@printvendo.test`
    created an account, granted it admin, and then could never be used.

    On a fresh production box this is the *only* way in, so getting it wrong
    locks you out of your own system. One such account is still sitting on the
    dev database from before this check existed.
    """
    with pytest.raises(BadRequest) as raised:
        bootstrap_admin(
            db_session, email="admin@printvendo.test", password="Str0ngEnough!"
        )

    assert "sign in" in str(raised.value.detail)
    assert identity_repo.get_by_email(db_session, "admin@printvendo.test") is None


def test_something_that_is_not_an_address_at_all_is_refused(db_session):
    with pytest.raises(BadRequest):
        bootstrap_admin(db_session, email="not-an-email", password="Str0ngEnough!")


def test_a_real_address_is_still_fine(db_session):
    """The check must not be so eager that it refuses the ordinary case."""
    user = bootstrap_admin(
        db_session, email="ops@printvendo.com", password="Str0ngEnough!"
    )

    assert Role.ADMIN in identity_repo.roles_of(db_session, user.id)
