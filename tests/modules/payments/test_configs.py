import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SecretBox
from app.core.errors import BadRequest, Conflict, NotFound
from app.core.ids import IdPrefix, parse_id
from app.modules.identity.models import User
from app.modules.payments.configs import (
    decrypt_secret,
    get_config,
    has_usable_keys,
    pending_change_requests,
    proof_key,
    request_change,
    review_change,
    review_change_by_id,
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


# ── what an admin reviews ───────────────────────────────────────────────────
# The half of this loop that was missing: an owner could ask, and nothing could
# answer. These are the reads and the one write an admin surface needs.


def test_a_request_has_an_opaque_public_id(db_session, owner):
    """Addressable by an admin route without exposing a primary key."""
    req = request_change(db_session, owner.id, reason="r", proof_path=None)
    db_session.flush()

    assert parse_id(req.public_id, IdPrefix.PAYMENT_CONFIG_CHANGE)


def test_pending_requests_are_listed_with_the_owner_named(db_session, owner):
    """An admin reviewing a request needs to know whose money it is about.
    The public id and the email come back on the view rather than being looked
    up per row by the caller."""
    request_change(db_session, owner.id, reason="moved banks", proof_path=None)
    db_session.flush()

    listed = pending_change_requests(db_session)

    assert len(listed) == 1
    assert listed[0].owner_public_id == owner.public_id
    assert listed[0].owner_email == owner.email
    assert listed[0].reason == "moved banks"


def test_a_reviewed_request_leaves_the_pending_list(db_session, owner, admin):
    req = request_change(db_session, owner.id, reason="r", proof_path=None)
    db_session.flush()
    review_change(db_session, req, approve=True, reviewer_user_id=admin.id)
    db_session.flush()

    assert pending_change_requests(db_session) == []


def test_the_view_carries_no_storage_key(db_session, owner):
    """`has_proof` rather than the key itself. A storage key in a JSON response
    is an invitation to build a URL out of it, which is exactly the mistake the
    proof route exists to prevent."""
    request_change(db_session, owner.id, reason="r", proof_path="proofs/2026/08/x.png")
    db_session.flush()

    view = pending_change_requests(db_session)[0]

    assert view.has_proof is True
    assert not hasattr(view, "proof_path")
    assert "proofs/2026/08/x.png" not in repr(view)


def test_a_request_can_be_reviewed_by_public_id(db_session, owner, admin):
    req = request_change(db_session, owner.id, reason="r", proof_path=None)
    db_session.flush()

    reviewed = review_change_by_id(
        db_session, req.public_id, approve=True, reviewer_user_id=admin.id, note="ok"
    )
    db_session.flush()

    assert reviewed.status is ChangeRequestStatus.APPROVED
    assert reviewed.owner_public_id == owner.public_id
    assert req.reviewed_by_user_id == admin.id


def test_reviewing_a_request_that_does_not_exist_is_not_found(db_session, admin):
    with pytest.raises(NotFound):
        review_change_by_id(
            db_session,
            "pcr_0000000000000000",
            approve=True,
            reviewer_user_id=admin.id,
        )


def test_the_proof_key_is_readable_only_through_the_service(db_session, owner):
    request_change(db_session, owner.id, reason="r", proof_path="proofs/2026/08/x.png")
    db_session.flush()
    public_id = pending_change_requests(db_session)[0].public_id

    assert proof_key(db_session, public_id) == "proofs/2026/08/x.png"


def test_a_request_with_no_proof_has_no_key_to_serve(db_session, owner):
    """404 rather than an empty body: "there is no proof" and "here is the
    proof, it is empty" must not look alike to an admin about to approve a
    change of bank details."""
    request_change(db_session, owner.id, reason="r", proof_path=None)
    db_session.flush()
    public_id = pending_change_requests(db_session)[0].public_id

    with pytest.raises(NotFound):
        proof_key(db_session, public_id)
