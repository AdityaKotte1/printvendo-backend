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


# ── telling somebody, once ──────────────────────────────────────────────────


class _Sent:
    """A notifier that remembers, and nothing else."""

    def __init__(self) -> None:
        self.offline: list[dict] = []

    def send_kiosk_offline(self, *, email, kiosk_name, last_seen):
        self.offline.append(
            {"email": email, "kiosk": kiosk_name, "last_seen": last_seen}
        )


@pytest.fixture
def sent(monkeypatch) -> _Sent:
    """Replaces the factory, not the adapter: the sweep builds its own notifier
    because the scheduler hands it only a session and settings."""
    box = _Sent()
    monkeypatch.setattr(tasks, "notifier_for", lambda settings, db: box)
    return box


def _admin(db_session, email: str, *, active: bool = True) -> User:
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.roles import Role

    person = User(email=email, hashed_password="x", is_active=active)
    db_session.add(person)
    db_session.flush()
    identity_repo.grant_role(db_session, person.id, Role.ADMIN)
    db_session.flush()
    return person


def test_a_shop_going_dark_writes_to_the_admins(db_session, settings, sent):
    kiosk = _kiosk(db_session)
    _device(db_session, kiosk, last_seen=NOW - timedelta(minutes=20))
    _admin(db_session, "operator@example.com")

    tasks.watch_offline_kiosks(db_session, settings)

    assert [m["email"] for m in sent.offline] == ["operator@example.com"]
    assert sent.offline[0]["kiosk"] == kiosk.name


def test_a_shop_still_dark_is_not_reported_again(db_session, settings, sent):
    """The sweep runs every five minutes. A shop that goes down at closing time
    would otherwise put a hundred identical emails in an inbox by morning --
    which is the wall of unread notifications the alerts table exists to avoid,
    reached by a different road."""
    kiosk = _kiosk(db_session)
    _device(db_session, kiosk, last_seen=NOW - timedelta(minutes=20))
    _admin(db_session, "operator@example.com")

    tasks.watch_offline_kiosks(db_session, settings)
    tasks.watch_offline_kiosks(db_session, settings)
    tasks.watch_offline_kiosks(db_session, settings)

    assert len(sent.offline) == 1


def test_a_shop_that_comes_back_and_goes_again_is_reported_again(
    db_session, settings, sent
):
    """The alert stands down when it comes back, so the next failure is a new
    row -- and a new row is a new email. An operator who fixed it at noon has
    to hear about it breaking again at four."""
    kiosk = _kiosk(db_session)
    device = _device(db_session, kiosk, last_seen=NOW - timedelta(minutes=20))
    _admin(db_session, "operator@example.com")

    tasks.watch_offline_kiosks(db_session, settings)

    device.last_heartbeat_at = datetime.now(UTC)
    db_session.flush()
    tasks.watch_offline_kiosks(db_session, settings)

    device.last_heartbeat_at = NOW - timedelta(minutes=20)
    db_session.flush()
    tasks.watch_offline_kiosks(db_session, settings)

    assert len(sent.offline) == 2


def test_a_working_shop_writes_to_nobody(db_session, settings, sent):
    kiosk = _kiosk(db_session)
    _device(db_session, kiosk, last_seen=NOW - timedelta(seconds=30))
    _admin(db_session, "operator@example.com")

    tasks.watch_offline_kiosks(db_session, settings)

    assert sent.offline == []


def test_a_switched_off_admin_is_not_written_to(db_session, settings, sent):
    """`holders_of` includes deactivated accounts on purpose -- switching
    somebody off is exactly when an operator needs to find them. That is right
    for a list on screen and wrong for a mailing: somebody who has been removed
    should stop hearing about the estate."""
    kiosk = _kiosk(db_session)
    _device(db_session, kiosk, last_seen=NOW - timedelta(minutes=20))
    _admin(db_session, "gone@example.com", active=False)
    _admin(db_session, "here@example.com")

    tasks.watch_offline_kiosks(db_session, settings)

    assert [m["email"] for m in sent.offline] == ["here@example.com"]


# ── work a dead device left holding ─────────────────────────────────────────


def _claimed_task(db_session, kiosk, user, *, lease_ends):
    """A task some device took and never reported on."""
    from app.modules.printing.models import PrintTask, TaskState

    document = Document(
        user_id=user.id,
        original_filename="stranded.pdf",
        page_count=2,
        original_path="originals/2026/09/stranded.pdf",
        state=DocumentState.READY,
    )
    db_session.add(document)
    db_session.flush()

    task = PrintTask(
        document_id=document.id,
        kiosk_id=kiosk.id,
        state=TaskState.SENT_TO_DEVICE,
        predicted_sheets=2,
        claimed_at=lease_ends - timedelta(minutes=15),
        lease_expires_at=lease_ends,
        attempts=1,
    )
    db_session.add(task)
    db_session.flush()
    return task


def test_a_job_a_dead_agent_was_holding_goes_back_in_the_queue(
    db_session, settings, user
):
    """`requeue_expired` was written, documented as the crash-recovery
    mechanism, exported -- and called by nothing. So an agent that died mid-job
    stranded that task in SENT_TO_DEVICE for ever: the claim only takes QUEUED,
    so no device could ever see it again, no report ever arrived, and the order
    sat at PAID permanently with nothing on any surface saying why.

    It happened repeatedly at one shop in an afternoon of restarts.
    """
    from app.modules.printing.models import TaskState

    kiosk = _kiosk(db_session)
    task = _claimed_task(db_session, kiosk, user, lease_ends=NOW - timedelta(minutes=1))

    tasks.recover_lost_tasks(db_session, settings)
    # Flushed before reading back: the sweep leaves the change pending for the
    # scheduler's own commit, and a bare refresh would re-read the row and
    # discard it -- proving nothing except that refresh works.
    db_session.flush()
    db_session.refresh(task)

    assert task.state is TaskState.QUEUED
    assert task.claimed_at is None
    assert task.lease_expires_at is None


def test_a_job_a_device_is_still_printing_is_left_alone(db_session, settings, user):
    """The lease is renewed on every progress report, so a live job always has
    one in the future. Requeueing it would hand the same job to a second
    device and print it twice."""
    from app.modules.printing.models import TaskState

    kiosk = _kiosk(db_session)
    task = _claimed_task(db_session, kiosk, user, lease_ends=NOW + timedelta(minutes=10))

    tasks.recover_lost_tasks(db_session, settings)
    # Flushed before reading back: the sweep leaves the change pending for the
    # scheduler's own commit, and a bare refresh would re-read the row and
    # discard it -- proving nothing except that refresh works.
    db_session.flush()
    db_session.refresh(task)

    assert task.state is TaskState.SENT_TO_DEVICE


def test_a_job_that_has_defeated_three_devices_is_failed_rather_than_retried(
    db_session, settings, user
):
    """A document that crashes the printer would otherwise be handed out for
    ever and block everything behind it."""
    from app.modules.printing.models import TaskState

    kiosk = _kiosk(db_session)
    task = _claimed_task(db_session, kiosk, user, lease_ends=NOW - timedelta(minutes=1))
    task.attempts = 3
    db_session.flush()

    tasks.recover_lost_tasks(db_session, settings)
    # Flushed before reading back: the sweep leaves the change pending for the
    # scheduler's own commit, and a bare refresh would re-read the row and
    # discard it -- proving nothing except that refresh works.
    db_session.flush()
    db_session.refresh(task)

    assert task.state is TaskState.FAILED
    assert task.error_code == "LEASE_EXPIRED"


def test_a_recovery_sweep_with_nothing_lost_says_nothing(db_session, settings):
    """It runs every minute and almost every run finds nothing. A sentence per
    tick would bury the ones that matter."""
    assert tasks.recover_lost_tasks(db_session, settings) == ""
