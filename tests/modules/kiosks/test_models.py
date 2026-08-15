import pytest
from sqlalchemy.exc import IntegrityError

from app.core.ids import IdPrefix, parse_id
from app.modules.identity.models import User
from app.modules.kiosks.enums import (
    AssignmentRole,
    DeviceStatus,
    KioskType,
    OnboardingStage,
)
from app.modules.kiosks.models import (
    Kiosk,
    KioskAssignment,
    KioskDevice,
    KioskPaper,
)


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    k = Kiosk(name="Library Ground Floor")
    db_session.add(k)
    db_session.flush()
    return k


@pytest.fixture
def user(db_session) -> User:
    u = User(email="owner@example.com", hashed_password="x")
    db_session.add(u)
    db_session.flush()
    return u


def test_kiosk_public_id_is_prefixed(db_session, kiosk):
    assert parse_id(kiosk.public_id, IdPrefix.KIOSK)


def test_a_new_kiosk_defaults_to_platform_and_registered(db_session, kiosk):
    assert kiosk.kiosk_type == KioskType.PLATFORM
    assert kiosk.onboarding_stage == OnboardingStage.REGISTERED


def test_a_new_kiosk_does_not_accept_wallet(db_session, kiosk):
    """Wallet top-ups land in the platform's account. Defaulting to True at an
    owner-gateway kiosk would mean the platform keeps the cash while the owner
    prints for free -- wrong permissively costs money, wrong restrictively
    costs a student one payment method."""
    assert kiosk.accepts_wallet is False


def test_kiosk_name_is_unique(db_session, kiosk):
    db_session.add(Kiosk(name=kiosk.name))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_device_public_id_is_prefixed(db_session, kiosk):
    device = KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="h")
    db_session.add(device)
    db_session.flush()
    assert parse_id(device.public_id, IdPrefix.DEVICE)


def test_device_defaults_to_offline(db_session, kiosk):
    device = KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="h")
    db_session.add(device)
    db_session.flush()
    assert device.status == DeviceStatus.OFFLINE


def test_device_key_is_unique(db_session, kiosk):
    db_session.add(KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="a"))
    db_session.flush()

    other = Kiosk(name="Other")
    db_session.add(other)
    db_session.flush()

    db_session.add(KioskDevice(kiosk_id=other.id, device_key="pi-001", token_hash="b"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_kiosk_has_at_most_one_device(db_session, kiosk):
    db_session.add(KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="a"))
    db_session.flush()
    db_session.add(KioskDevice(kiosk_id=kiosk.id, device_key="pi-002", token_hash="b"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_device_table_stores_no_plaintext_token():
    columns = set(KioskDevice.__table__.columns.keys())
    assert "token_hash" in columns
    assert not {"token", "secret_token", "secret"} & columns


def test_paper_is_one_row_per_kiosk(db_session, kiosk):
    db_session.add(KioskPaper(kiosk_id=kiosk.id))
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_paper_defaults_to_a_full_250_sheet_tray(db_session, kiosk):
    paper = KioskPaper(kiosk_id=kiosk.id)
    db_session.add(paper)
    db_session.flush()
    assert paper.capacity == 250
    assert paper.used == 0


def test_a_user_cannot_hold_the_same_role_at_one_kiosk_twice(db_session, kiosk, user):
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.OWNER)
    )
    db_session.flush()
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.OWNER)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_user_may_be_owner_and_refiller_at_one_kiosk(db_session, kiosk, user):
    """A small shop's owner refills their own paper."""
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.OWNER)
    )
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.REFILLER)
    )
    db_session.flush()


def test_legacy_id_exists_for_migration(db_session):
    k = Kiosk(name="Old One", legacy_id=17)
    db_session.add(k)
    db_session.flush()
    assert k.legacy_id == 17


def test_prices_are_decimal_not_float(db_session, kiosk):
    """Money is Decimal everywhere. A float price would round wrong at scale."""
    from decimal import Decimal

    kiosk.price_bw_single = Decimal("2.50")
    db_session.flush()
    db_session.refresh(kiosk)
    assert isinstance(kiosk.price_bw_single, Decimal)
