"""What each scheduled sweep does, and what it deliberately leaves alone.

The two retention jobs are covered thoroughly at the module level already, so
what is tested here is the wiring -- that the job reaches the function that had
no caller. The two watchers are new, and get the detail.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.jobs import tasks
from app.modules.identity.models import User
from app.modules.kiosks.enums import DeviceStatus, KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskDevice, KioskPaper
from app.modules.ops import AlertSeverity, open_alerts
from app.modules.orders.models import Order, OrderState, PaymentMethod
from app.modules.printing.models import Document, DocumentState
from app.modules.printing.storage import DocumentStore, StorageArea

NOW = datetime.now(UTC)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        ENV="dev",
        DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
        REDIS_URL="redis://localhost:6379/0",
        JWT_SECRET_KEY="s" * 32,
        SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        STORAGE_ROOT=str(tmp_path / "storage"),
        CORS_ORIGINS="http://localhost:3000",
    )


@pytest.fixture
def user(db_session) -> User:
    user = User(email="sweeps@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


def _kiosk(
    db_session,
    name: str = "Sweep Shop",
    *,
    stage: OnboardingStage = OnboardingStage.LIVE,
    sheets: int = 500,
) -> Kiosk:
    kiosk = Kiosk(
        name=name,
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=stage,
        price_bw_single=Decimal("2.00"),
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=500 - sheets))
    db_session.flush()
    return kiosk


def _device(db_session, kiosk: Kiosk, *, last_seen: datetime | None) -> KioskDevice:
    device = KioskDevice(
        kiosk_id=kiosk.id,
        device_key=f"dev-{kiosk.id}",
        token_hash="x" * 64,
        status=DeviceStatus.ONLINE,
        last_heartbeat_at=last_seen,
    )
    db_session.add(device)
    db_session.flush()
    return device


# ── expiring orders ─────────────────────────────────────────────────────────


def test_the_expiry_job_reaches_the_function_nothing_was_calling(
    db_session, settings, user
):
    kiosk = _kiosk(db_session)
    order = Order(
        user_id=user.id,
        kiosk_id=kiosk.id,
        state=OrderState.AWAITING_PAYMENT,
        payment_method=PaymentMethod.GATEWAY,
        subtotal_inr=Decimal("10.00"),
        fee_inr=Decimal("0.00"),
        total_inr=Decimal("10.00"),
        expires_at=NOW - timedelta(minutes=1),
    )
    db_session.add(order)
    db_session.flush()

    summary = tasks.expire_orders(db_session, settings)

    assert order.state is OrderState.EXPIRED
    assert "1" in summary


def test_a_quiet_sweep_says_nothing(db_session, settings):
    """An hourly job that logs "expired 0 orders" for ever teaches people to
    filter it out, and they filter out the line that mattered with it."""
    assert tasks.expire_orders(db_session, settings) == ""


# ── purging files ───────────────────────────────────────────────────────────


def test_the_purge_job_reaches_the_function_nothing_was_calling(
    db_session, settings, user
):
    store = DocumentStore(settings.STORAGE_ROOT)
    key = store.new_key(StorageArea.ORIGINAL, user_id=user.id, filename="a.pdf")
    store.write(key, b"%PDF-1.4 something")

    document = Document(
        user_id=user.id,
        original_filename="a.pdf",
        original_path=key,
        state=DocumentState.READY,
        created_at=NOW - timedelta(days=30),
    )
    db_session.add(document)
    db_session.flush()

    tasks.purge_files(db_session, settings)

    assert document.state is DocumentState.EXPIRED
    assert not store.exists(key)


# ── kiosks that have gone dark ──────────────────────────────────────────────


def test_a_kiosk_that_has_not_reported_raises_an_alert(db_session, settings):
    kiosk = _kiosk(db_session)
    _device(db_session, kiosk, last_seen=NOW - timedelta(minutes=20))

    tasks.watch_offline_kiosks(db_session, settings)

    alerts = open_alerts(db_session)
    assert [a.kind for a in alerts] == ["kiosk.offline"]
    assert alerts[0].entity_id == kiosk.public_id


def test_a_kiosk_that_reported_a_moment_ago_raises_nothing(db_session, settings):
    kiosk = _kiosk(db_session)
    _device(db_session, kiosk, last_seen=NOW - timedelta(seconds=30))

    tasks.watch_offline_kiosks(db_session, settings)

    assert open_alerts(db_session) == []


def test_a_kiosk_that_comes_back_closes_its_own_alert(db_session, settings):
    """A machine noticed it; the same machine can see it end.

    Without this the console fills with shops that were briefly offline last
    week, and an operator learns to ignore the whole page.
    """
    kiosk = _kiosk(db_session)
    device = _device(db_session, kiosk, last_seen=NOW - timedelta(minutes=20))
    tasks.watch_offline_kiosks(db_session, settings)

    device.last_heartbeat_at = datetime.now(UTC)
    db_session.flush()
    tasks.watch_offline_kiosks(db_session, settings)

    assert open_alerts(db_session) == []


def test_a_shop_dark_for_an_hour_is_more_serious_than_one_dark_for_ten_minutes(
    db_session, settings
):
    recent = _kiosk(db_session, "Blip Shop")
    _device(db_session, recent, last_seen=NOW - timedelta(minutes=10))
    gone = _kiosk(db_session, "Dark Shop")
    _device(db_session, gone, last_seen=NOW - timedelta(hours=3))

    tasks.watch_offline_kiosks(db_session, settings)

    by_kiosk = {a.entity_id: a.severity for a in open_alerts(db_session)}
    assert by_kiosk[recent.public_id] is AlertSeverity.WARNING
    assert by_kiosk[gone.public_id] is AlertSeverity.CRITICAL


def test_a_live_kiosk_with_no_device_at_all_is_reported(db_session, settings):
    """It is selling and it cannot print. That is worse than offline, not better."""
    _kiosk(db_session)

    tasks.watch_offline_kiosks(db_session, settings)

    alerts = open_alerts(db_session)
    assert [a.severity for a in alerts] == [AlertSeverity.CRITICAL]


def test_a_kiosk_that_is_not_live_yet_is_not_reported(db_session, settings):
    """Somebody's half-finished setup is not an incident."""
    _kiosk(db_session, stage=OnboardingStage.REGISTERED)

    tasks.watch_offline_kiosks(db_session, settings)

    assert open_alerts(db_session) == []


def test_repeating_the_sweep_does_not_repeat_the_alert(db_session, settings):
    kiosk = _kiosk(db_session)
    _device(db_session, kiosk, last_seen=NOW - timedelta(minutes=20))

    tasks.watch_offline_kiosks(db_session, settings)
    tasks.watch_offline_kiosks(db_session, settings)

    alerts = open_alerts(db_session)
    assert len(alerts) == 1
    assert alerts[0].occurrences == 2


# ── paper ───────────────────────────────────────────────────────────────────


def test_an_empty_tray_is_critical(db_session, settings):
    _kiosk(db_session, sheets=0)

    tasks.watch_paper(db_session, settings)

    alerts = open_alerts(db_session)
    assert [a.severity for a in alerts] == [AlertSeverity.CRITICAL]


def test_a_low_tray_is_a_warning(db_session, settings):
    _kiosk(db_session, sheets=20)

    tasks.watch_paper(db_session, settings)

    assert [a.severity for a in open_alerts(db_session)] == [AlertSeverity.WARNING]


def test_a_full_tray_says_nothing(db_session, settings):
    _kiosk(db_session, sheets=400)

    tasks.watch_paper(db_session, settings)

    assert open_alerts(db_session) == []


def test_a_tray_that_empties_after_running_low_escalates_one_alert(db_session, settings):
    """Two rows would make "how long has this shop been out" have two answers."""
    kiosk = _kiosk(db_session, sheets=20)
    tasks.watch_paper(db_session, settings)

    paper = db_session.get(KioskPaper, kiosk.id)
    paper.used = paper.capacity
    db_session.flush()
    tasks.watch_paper(db_session, settings)

    alerts = open_alerts(db_session)
    assert len(alerts) == 1
    assert alerts[0].severity is AlertSeverity.CRITICAL


def test_a_refilled_tray_closes_its_own_alert(db_session, settings):
    kiosk = _kiosk(db_session, sheets=0)
    tasks.watch_paper(db_session, settings)

    paper = db_session.get(KioskPaper, kiosk.id)
    paper.used = 0
    db_session.flush()
    tasks.watch_paper(db_session, settings)

    assert open_alerts(db_session) == []


def test_paper_is_only_watched_where_something_is_selling(db_session, settings):
    _kiosk(db_session, stage=OnboardingStage.APPROVED, sheets=0)

    tasks.watch_paper(db_session, settings)

    assert open_alerts(db_session) == []
