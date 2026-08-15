from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.ids import IdPrefix, parse_id
from app.modules.identity.models import RefreshToken, User, UserRole
from app.modules.identity.roles import Role


def test_role_enum_has_exactly_the_four_roles():
    assert {r.value for r in Role} == {"admin", "owner", "refiller", "student"}


def test_user_public_id_is_generated_and_prefixed(db_session):
    user = User(email="a@b.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    assert parse_id(user.public_id, IdPrefix.USER)


def test_two_users_get_different_public_ids(db_session):
    first = User(email="a@b.test", hashed_password="x")
    second = User(email="c@d.test", hashed_password="x")
    db_session.add_all([first, second])
    db_session.flush()
    assert first.public_id != second.public_id


def test_user_defaults(db_session):
    user = User(email="a@b.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    assert user.is_active is True
    assert user.is_guest is False
    assert user.created_at is not None


def test_email_is_unique(db_session):
    db_session.add(User(email="dup@b.test", hashed_password="x"))
    db_session.flush()
    db_session.add(User(email="dup@b.test", hashed_password="y"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_user_cannot_hold_the_same_role_twice(db_session):
    user = User(email="r@b.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    db_session.add(UserRole(user_id=user.id, role=Role.STUDENT))
    db_session.flush()
    db_session.add(UserRole(user_id=user.id, role=Role.STUDENT))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_refresh_token_table_has_no_column_that_could_hold_a_raw_token():
    """The table must be unable to store a usable token, not merely avoid it.

    Asserting on the column set rather than on an instance attribute: a model
    without the attribute proves nothing, since any attribute is absent until
    something adds it.
    """
    columns = set(RefreshToken.__table__.columns.keys())
    assert "token_hash" in columns
    assert not {"token", "raw_token", "secret", "value"} & columns


def test_refresh_token_hash_is_unique(db_session):
    user = User(email="t@b.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    expires = datetime.now(UTC) + timedelta(days=30)
    db_session.add(
        RefreshToken(
            user_id=user.id, token_hash="deadbeef", family_id="fam_1", expires_at=expires
        )
    )
    db_session.flush()
    db_session.add(
        RefreshToken(
            user_id=user.id, token_hash="deadbeef", family_id="fam_2", expires_at=expires
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_legacy_id_column_exists_for_migration(db_session):
    user = User(email="l@b.test", hashed_password="x", legacy_id=4321)
    db_session.add(user)
    db_session.flush()
    assert user.legacy_id == 4321


def test_the_database_refuses_emails_differing_only_by_case(db_session):
    """Production has ten such pairs, each with its own wallet balance.

    The service layer lowercases before comparing, but that cannot hold on its
    own: two concurrent registrations both pass the check and both insert. This
    asserts the *database* refuses it, which is the only version that survives a
    race or a direct insert.
    """
    db_session.add(User(email="Person@Example.com", hashed_password="x"))
    db_session.flush()

    db_session.add(User(email="person@example.com", hashed_password="y"))
    with pytest.raises(IntegrityError):
        db_session.flush()
