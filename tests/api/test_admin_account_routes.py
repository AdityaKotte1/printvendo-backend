"""Finding a person, and deciding what they may do.

The last thing on the admin surface that could only be done in SQL. Roles are
rows here rather than three booleans on the user, which is what stops a refiller
being one forgotten check away from money data -- but until now nothing could
write those rows except an accepted kiosk invitation. Nobody could be made an
admin, and nobody could be un-made one.

Two properties carry the weight:

* **Deactivating an account signs it out.** A disabled row whose access token
  keeps working for another quarter of an hour is a dismissal that has not
  happened yet.
* **The search is not a directory.** It answers on an exact address or not at
  all, so an admin console cannot be walked to enumerate the platform's users.
"""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.core.config import Settings
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.ops import entries_for

SECRET = "s" * 32
BOX_KEY = Fernet.generate_key().decode()

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=BOX_KEY,
    CORS_ORIGINS="https://admin.printvendo.com",
    PUBLIC_BASE_URL="https://api.printvendo.com",
)

ACCOUNTS = "/v1/admin/accounts"


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    return TestClient(app, raise_server_exceptions=False)


def _user(db_session, email: str, *roles: Role) -> User:
    user = User(email=email, hashed_password="x")
    db_session.add(user)
    db_session.flush()
    for role in roles:
        identity_repo.grant_role(db_session, user.id, role)
    db_session.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    token = create_token(user.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db_session) -> User:
    return _user(db_session, "ops@printvendo.com", Role.ADMIN)


@pytest.fixture
def admin_auth(admin) -> dict[str, str]:
    return _auth(admin)


@pytest.fixture
def person(db_session) -> User:
    return _user(db_session, "shopkeeper@example.com", Role.STUDENT)


# -- finding somebody -------------------------------------------------------


def test_an_account_is_found_by_its_exact_address(client, admin_auth, person):
    found = client.get(f"{ACCOUNTS}?email={person.email}", headers=admin_auth).json()

    assert [a["id"] for a in found] == [person.public_id]
    assert found[0]["roles"] == ["student"]
    assert found[0]["is_active"] is True


def test_the_search_matches_case_insensitively(client, admin_auth, person):
    """Postgres compares case-sensitively, so an admin typing an address with a
    capital would otherwise be told the account does not exist -- which is how
    the duplicate accounts in the legacy data were created in the first place."""
    found = client.get(f"{ACCOUNTS}?email=SHOPKEEPER@EXAMPLE.COM", headers=admin_auth)

    assert [a["id"] for a in found.json()] == [person.public_id]


def test_a_partial_address_finds_nobody(client, admin_auth, person):
    """Exact match only. A console that answers prefixes is a directory of every
    address on the platform, walkable by anyone who reaches it."""
    found = client.get(f"{ACCOUNTS}?email=shopkeeper", headers=admin_auth)

    assert found.json() == []


def test_asking_for_nobody_in_particular_is_refused(client, admin_auth, person):
    """No "list every user" answer, even for an admin. Naming a role is not
    that -- see the listing tests at the foot of this file."""
    assert client.get(ACCOUNTS, headers=admin_auth).status_code == 400


def test_an_account_never_carries_its_password_hash(client, admin_auth, person):
    body = client.get(f"{ACCOUNTS}/{person.public_id}", headers=admin_auth).text

    assert "hashed_password" not in body
    assert "hash" not in body


def test_an_unknown_account_is_not_found(client, admin_auth):
    response = client.get(f"{ACCOUNTS}/usr_0000000000000000", headers=admin_auth)

    assert response.status_code == 404


def test_an_id_of_the_wrong_kind_is_also_not_found(client, admin_auth):
    """`ksk_...` where a user id belongs resolves to nothing rather than to
    whatever happens to share the number."""
    response = client.get(f"{ACCOUNTS}/ksk_0000000000000000", headers=admin_auth)

    assert response.status_code == 404


# -- roles ------------------------------------------------------------------


def test_a_role_can_be_granted_and_taken_away(client, admin_auth, person, db_session):
    granted = client.put(
        f"{ACCOUNTS}/{person.public_id}/roles/refiller", headers=admin_auth
    )

    assert granted.status_code == 200
    assert set(granted.json()["roles"]) == {"student", "refiller"}

    revoked = client.delete(
        f"{ACCOUNTS}/{person.public_id}/roles/refiller", headers=admin_auth
    )

    assert revoked.json()["roles"] == ["student"]
    assert Role.REFILLER not in identity_repo.roles_of(db_session, person.id)


def test_granting_a_role_twice_is_not_an_error(client, admin_auth, person):
    """Two admins doing the same obvious thing is ordinary."""
    client.put(f"{ACCOUNTS}/{person.public_id}/roles/refiller", headers=admin_auth)
    again = client.put(
        f"{ACCOUNTS}/{person.public_id}/roles/refiller", headers=admin_auth
    )

    assert again.status_code == 200
    assert again.json()["roles"].count("refiller") == 1


def test_a_role_that_is_not_a_role_is_refused(client, admin_auth, person):
    response = client.put(
        f"{ACCOUNTS}/{person.public_id}/roles/superuser", headers=admin_auth
    )

    assert response.status_code == 400


def test_an_admin_cannot_revoke_their_own_admin_role(client, admin_auth, admin):
    """The one that locks everybody out. There is no second surface that can
    grant it back, so an admin removing their own last privilege ends with a
    platform nobody can administer."""
    response = client.delete(
        f"{ACCOUNTS}/{admin.public_id}/roles/admin", headers=admin_auth
    )

    assert response.status_code == 400
    assert "admin" in response.json()["detail"].lower()


def test_an_admin_may_still_revoke_another_admins_role(client, admin_auth, db_session):
    other = _user(db_session, "leaver@printvendo.com", Role.ADMIN)

    response = client.delete(
        f"{ACCOUNTS}/{other.public_id}/roles/admin", headers=admin_auth
    )

    assert response.status_code == 200
    assert response.json()["roles"] == []


# -- switching an account off ------------------------------------------------


def test_deactivating_an_account_signs_it_out_immediately(
    client, admin_auth, person, db_session
):
    """A disabled row whose token keeps working for another fifteen minutes is a
    dismissal that has not happened yet. `get_current_user` refuses an inactive
    account, so the access token dies with the row rather than with its clock."""
    person_auth = _auth(person)
    assert client.get("/v1/app/auth/me", headers=person_auth).status_code == 200

    client.post(f"{ACCOUNTS}/{person.public_id}/deactivate", headers=admin_auth)

    assert client.get("/v1/app/auth/me", headers=person_auth).status_code == 401


def test_a_deactivated_account_can_be_let_back_in(client, admin_auth, person):
    client.post(f"{ACCOUNTS}/{person.public_id}/deactivate", headers=admin_auth)

    reactivated = client.post(
        f"{ACCOUNTS}/{person.public_id}/activate", headers=admin_auth
    )

    assert reactivated.json()["is_active"] is True
    assert client.get("/v1/app/auth/me", headers=_auth(person)).status_code == 200


def test_an_admin_cannot_deactivate_themselves(client, admin_auth, admin):
    response = client.post(f"{ACCOUNTS}/{admin.public_id}/deactivate", headers=admin_auth)

    assert response.status_code == 400


# -- who may do this --------------------------------------------------------


def test_none_of_this_is_reachable_by_an_owner(client, db_session, person):
    """An owner who could grant roles could make themselves an admin, and an
    owner who could search accounts has a list of every student on the
    platform."""
    owner_auth = _auth(_user(db_session, "owner@example.com", Role.OWNER))

    assert client.get(f"{ACCOUNTS}?email=a@b.com", headers=owner_auth).status_code == 403
    assert (
        client.put(
            f"{ACCOUNTS}/{person.public_id}/roles/admin", headers=owner_auth
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{ACCOUNTS}/{person.public_id}/deactivate", headers=owner_auth
        ).status_code
        == 403
    )


def test_the_account_search_needs_a_token(client):
    assert client.get(f"{ACCOUNTS}?email=a@b.com").status_code == 401


# -- the trail --------------------------------------------------------------


def test_role_and_status_changes_are_audited(
    client, admin_auth, admin, person, db_session
):
    """Who made this person an admin, and when, is the first question after an
    account does something it should not have been able to."""
    client.put(f"{ACCOUNTS}/{person.public_id}/roles/admin", headers=admin_auth)
    client.post(f"{ACCOUNTS}/{person.public_id}/deactivate", headers=admin_auth)

    trail = entries_for(db_session, entity_type="user", entity_id=person.public_id)
    actions = [e.action for e in trail]

    assert "identity.role.granted" in actions
    assert "identity.account.deactivated" in actions
    granted = next(e for e in trail if e.action == "identity.role.granted")
    assert granted.after["role"] == "admin"
    assert granted.actor_user_id == admin.id


# ── listing by role ─────────────────────────────────────────────────────────
#
# The exact-address rule exists so this console cannot become a directory of
# every student on the platform. Owners, refillers and admins are a different
# set: a handful of people the operator administers, already named on kiosks
# the same admin can list, and remembering ten addresses to look after ten
# shops is not security -- it is a console nobody can use.
#
# STUDENT stays unlistable, because that *is* the directory the rule is about.


def test_owners_can_be_listed(client, admin_auth, db_session):
    _user(db_session, "shop.one@example.com", Role.OWNER)
    _user(db_session, "shop.two@example.com", Role.OWNER)
    _user(db_session, "a.student@example.com", Role.STUDENT)

    response = client.get(f"{ACCOUNTS}?role=owner", headers=admin_auth)

    assert response.status_code == 200
    assert {a["email"] for a in response.json()} == {
        "shop.one@example.com",
        "shop.two@example.com",
    }


def test_a_listed_owner_carries_the_same_card_as_a_search(
    client, admin_auth, db_session
):
    """One shape, so the console renders one card either way."""
    _user(db_session, "shop.one@example.com", Role.OWNER)

    listed = client.get(f"{ACCOUNTS}?role=owner", headers=admin_auth).json()
    searched = client.get(
        f"{ACCOUNTS}?email=shop.one@example.com", headers=admin_auth
    ).json()

    assert listed == searched


def test_students_cannot_be_listed(client, admin_auth, db_session):
    _user(db_session, "a.student@example.com", Role.STUDENT)

    response = client.get(f"{ACCOUNTS}?role=student", headers=admin_auth)

    assert response.status_code == 400
    assert "exact" in response.json()["detail"]


def test_a_deactivated_owner_is_still_listed(client, admin_auth, db_session):
    """Switching somebody off is exactly when an operator needs to find them."""
    stopped = _user(db_session, "stopped@example.com", Role.OWNER)
    stopped.is_active = False
    db_session.flush()

    response = client.get(f"{ACCOUNTS}?role=owner", headers=admin_auth)

    assert [a["is_active"] for a in response.json()] == [False]


def test_an_unknown_role_is_refused(client, admin_auth):
    response = client.get(f"{ACCOUNTS}?role=wizard", headers=admin_auth)

    assert response.status_code == 400


def test_a_student_still_cannot_reach_the_listing(client, db_session):
    nosy = _user(db_session, "nosy@example.com", Role.STUDENT)

    response = client.get(f"{ACCOUNTS}?role=owner", headers=_auth(nosy))

    assert response.status_code == 403
