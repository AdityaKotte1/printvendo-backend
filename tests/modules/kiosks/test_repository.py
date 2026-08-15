import pytest

from app.core.errors import NotFound
from app.modules.kiosks import repository as repo
from app.modules.kiosks.models import Kiosk
from app.modules.kiosks.scope import Scope

ADMIN = Scope(is_unrestricted=True, kiosk_ids=frozenset())
NOTHING = Scope(is_unrestricted=False, kiosk_ids=frozenset())


@pytest.fixture
def kiosks(db_session) -> tuple[Kiosk, Kiosk]:
    a, b = Kiosk(name="Alice Shop"), Kiosk(name="Bob Shop")
    db_session.add_all([a, b])
    db_session.flush()
    return a, b


def _only(kiosk: Kiosk) -> Scope:
    return Scope(is_unrestricted=False, kiosk_ids=frozenset({kiosk.id}))


def test_list_returns_only_scoped_kiosks(db_session, kiosks):
    a, _ = kiosks
    assert [k.id for k in repo.list_kiosks(db_session, _only(a))] == [a.id]


def test_list_for_an_admin_returns_everything(db_session, kiosks):
    assert len(repo.list_kiosks(db_session, ADMIN)) == 2


def test_list_for_an_empty_scope_returns_nothing(db_session, kiosks):
    assert repo.list_kiosks(db_session, NOTHING) == []


def test_get_returns_a_kiosk_in_scope(db_session, kiosks):
    a, _ = kiosks
    assert repo.get_kiosk(db_session, _only(a), a.public_id).id == a.id


def test_get_raises_not_found_for_a_kiosk_outside_scope(db_session, kiosks):
    """404 rather than 403: a 403 confirms the kiosk exists, which is itself a
    disclosure to someone who has no business knowing."""
    a, b = kiosks
    with pytest.raises(NotFound):
        repo.get_kiosk(db_session, _only(a), b.public_id)


def test_get_raises_not_found_for_an_unknown_id(db_session, kiosks):
    with pytest.raises(NotFound):
        repo.get_kiosk(db_session, ADMIN, "ksk_0000000000000000")


def test_get_raises_not_found_for_an_id_of_the_wrong_kind(db_session, kiosks):
    a, _ = kiosks
    with pytest.raises(NotFound):
        repo.get_kiosk(db_session, ADMIN, a.public_id.replace("ksk_", "usr_"))


def test_get_raises_not_found_for_a_malformed_id(db_session):
    with pytest.raises(NotFound):
        repo.get_kiosk(db_session, ADMIN, "not-an-id")


def test_the_message_is_identical_inside_and_outside_scope(db_session, kiosks):
    """Otherwise the wording tells an owner whether someone else's kiosk id is
    real."""
    a, b = kiosks

    with pytest.raises(NotFound) as outside:
        repo.get_kiosk(db_session, _only(a), b.public_id)
    with pytest.raises(NotFound) as unknown:
        repo.get_kiosk(db_session, _only(a), "ksk_0000000000000000")

    assert str(outside.value) == str(unknown.value)


def test_an_admin_can_get_any_kiosk(db_session, kiosks):
    a, b = kiosks
    assert repo.get_kiosk(db_session, ADMIN, a.public_id).id == a.id
    assert repo.get_kiosk(db_session, ADMIN, b.public_id).id == b.id


def test_inactive_kiosks_are_excluded_by_default(db_session, kiosks):
    a, _ = kiosks
    a.is_active = False
    db_session.flush()
    assert [k.id for k in repo.list_kiosks(db_session, ADMIN)] == [
        k.id for k in kiosks if k.is_active
    ]


def test_inactive_kiosks_can_be_asked_for_explicitly(db_session, kiosks):
    a, _ = kiosks
    a.is_active = False
    db_session.flush()
    assert len(repo.list_kiosks(db_session, ADMIN, include_inactive=True)) == 2


def test_listing_is_ordered_by_name(db_session):
    for name in ("Zulu", "Alpha", "Mike"):
        db_session.add(Kiosk(name=name))
    db_session.flush()
    names = [k.name for k in repo.list_kiosks(db_session, ADMIN)]
    assert names == sorted(names)
