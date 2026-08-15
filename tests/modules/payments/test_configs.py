import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SecretBox
from app.core.errors import BadRequest, Conflict, NotFound
from app.modules.identity.models import User
from app.modules.payments.configs import (
    decrypt_secret,
    get_config,
    has_usable_keys,
    request_change,
    review_change,
    set_keys,
    view_config,
)
from app.modules.payments.models import ChangeRequestStatus, KioskPaymentConfig

BOX = SecretBox(Fernet.generate_key().decode())
KEY_ID = "rzp_live_ownerkey123"
SECRET = "supersecretvalue"


@pytest.fixture
def owner(db_session) -> User:
    user = User(email="owner@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def admin(db_session) -> User:
    user = User(email="admin@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


# ── encryption ──────────────────────────────────────────────────────────────


def test_the_secret_is_not_stored_in_plaintext(db_session, owner):
    """The defect this module exists to fix: the old backend stored this in the
    clear while claiming otherwise, so any database dump leaked live payment
    credentials."""
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    db_session.flush()

    stored = db_session.query(KioskPaymentConfig).one()
    assert SECRET not in (stored.razorpay_key_secret_encrypted or "")
    assert stored.razorpay_key_secret_encrypted != SECRET


def test_the_table_has_no_plaintext_secret_column():
    columns = set(KioskPaymentConfig.__table__.columns.keys())
    assert "razorpay_key_secret_encrypted" in columns
    assert "razorpay_key_secret" not in columns


def test_the_secret_round_trips_for_the_payment_path(db_session, owner):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    db_session.flush()

    assert decrypt_secret(get_config(db_session, owner.id), BOX) == SECRET


def test_a_different_key_cannot_decrypt(db_session, owner):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    db_session.flush()

    other = SecretBox(Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        decrypt_secret(get_config(db_session, owner.id), other)


def test_decrypting_an_unconfigured_account_raises(db_session, owner):
    config = KioskPaymentConfig(user_id=owner.id)
    db_session.add(config)
    db_session.flush()

    with pytest.raises(NotFound):
        decrypt_secret(config, BOX)


# ── what the API may say ────────────────────────────────────────────────────


def test_an_unconfigured_account_reads_as_unconfigured(db_session, owner):
    view = view_config(db_session, owner.id)
    assert view.is_configured is False
    assert view.key_id_masked is None
    assert view.can_update is True


def test_the_view_never_contains_the_secret(db_session, owner):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    db_session.flush()

    view = view_config(db_session, owner.id)
    assert SECRET not in repr(view)
    assert not hasattr(view, "key_secret")


def test_the_key_id_is_masked(db_session, owner):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    db_session.flush()

    masked = view_config(db_session, owner.id).key_id_masked
    assert masked == "••••y123"
    assert KEY_ID not in masked


# ── set-once ────────────────────────────────────────────────────────────────


def test_keys_can_be_set_when_nothing_is_configured(db_session, owner):
    config = set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    assert config.is_configured is True


def test_keys_cannot_be_replaced_without_an_approved_request(db_session, owner):
    """Otherwise taking over an owner's account silently redirects every
    student payment at every one of their kiosks."""
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    db_session.flush()

    with pytest.raises(Conflict):
        set_keys(db_session, owner.id, key_id="rzp_live_attacker", key_secret="x", box=BOX)


def test_a_pending_request_does_not_authorise_a_change(db_session, owner):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    request_change(db_session, owner.id, reason="new bank", proof_path=None)
    db_session.flush()

    with pytest.raises(Conflict):
        set_keys(db_session, owner.id, key_id="rzp_live_new", key_secret="y", box=BOX)


def test_a_rejected_request_does_not_authorise_a_change(db_session, owner, admin):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    req = request_change(db_session, owner.id, reason="new bank", proof_path=None)
    db_session.flush()
    review_change(db_session, req, approve=False, reviewer_user_id=admin.id)
    db_session.flush()

    with pytest.raises(Conflict):
        set_keys(db_session, owner.id, key_id="rzp_live_new", key_secret="y", box=BOX)


def test_an_approved_request_allows_exactly_one_change(db_session, owner, admin):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    req = request_change(db_session, owner.id, reason="new bank", proof_path=None)
    db_session.flush()
    review_change(db_session, req, approve=True, reviewer_user_id=admin.id)
    db_session.flush()

    set_keys(db_session, owner.id, key_id="rzp_live_new", key_secret="second", box=BOX)
    db_session.flush()
    assert decrypt_secret(get_config(db_session, owner.id), BOX) == "second"

    # The approval is spent -- a second change needs a second approval.
    with pytest.raises(Conflict):
        set_keys(db_session, owner.id, key_id="rzp_live_third", key_secret="z", box=BOX)


def test_using_an_approval_marks_it_used(db_session, owner, admin):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    req = request_change(db_session, owner.id, reason="r", proof_path=None)
    db_session.flush()
    review_change(db_session, req, approve=True, reviewer_user_id=admin.id)
    db_session.flush()

    set_keys(db_session, owner.id, key_id="rzp_live_new", key_secret="second", box=BOX)
    db_session.flush()
    assert req.status is ChangeRequestStatus.USED


def test_blank_keys_are_refused(db_session, owner):
    with pytest.raises(BadRequest):
        set_keys(db_session, owner.id, key_id="  ", key_secret=SECRET, box=BOX)
    with pytest.raises(BadRequest):
        set_keys(db_session, owner.id, key_id=KEY_ID, key_secret="  ", box=BOX)


# ── change requests ─────────────────────────────────────────────────────────


def test_only_one_pending_request_at_a_time(db_session, owner):
    request_change(db_session, owner.id, reason="one", proof_path=None)
    db_session.flush()
    with pytest.raises(Conflict):
        request_change(db_session, owner.id, reason="two", proof_path=None)


def test_a_request_cannot_be_reviewed_twice(db_session, owner, admin):
    req = request_change(db_session, owner.id, reason="r", proof_path=None)
    db_session.flush()
    review_change(db_session, req, approve=True, reviewer_user_id=admin.id)
    db_session.flush()

    with pytest.raises(Conflict):
        review_change(db_session, req, approve=False, reviewer_user_id=admin.id)


def test_review_records_who_decided(db_session, owner, admin):
    req = request_change(db_session, owner.id, reason="r", proof_path=None)
    db_session.flush()
    review_change(db_session, req, approve=True, reviewer_user_id=admin.id, note="ok")
    db_session.flush()

    assert req.reviewed_by_user_id == admin.id
    assert req.reviewed_at is not None
    assert req.review_note == "ok"


# ── the gate's key half ─────────────────────────────────────────────────────


def test_has_usable_keys_is_false_before_configuration(db_session, owner):
    assert has_usable_keys(db_session, owner.id) is False


def test_has_usable_keys_is_true_once_configured(db_session, owner):
    set_keys(db_session, owner.id, key_id=KEY_ID, key_secret=SECRET, box=BOX)
    db_session.flush()
    assert has_usable_keys(db_session, owner.id) is True
