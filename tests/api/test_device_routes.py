"""The device API, over real HTTP.

The service tests prove the claim, the paper rules and the device token behave.
These prove the wiring: that one kiosk's Pi cannot reach another's work, that a
claimed task really is gone from the queue, and that the file a device is handed
is the whole document.
"""

import io
from datetime import timedelta

import pikepdf
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_document_store, get_notifier, get_secret
from app.core.config import Settings
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.devices import issue_enrolment_code, register_device
from app.modules.kiosks.enums import AssignmentRole
from app.modules.kiosks.models import KioskAssignment
from app.modules.kiosks.paper import set_paper
from app.modules.kiosks.registry import create_kiosk
from app.modules.printing.documents import create_document
from app.modules.printing.models import PrintTask, TaskState
from app.modules.printing.storage import DocumentStore

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY="k" * 44,
    CORS_ORIGINS="http://localhost:3000",
)


def make_pdf(pages: int = 2) -> bytes:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(tmp_path / "storage")


@pytest.fixture
def client(db_session, store) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    app.dependency_overrides[get_document_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def _auth(user: User) -> dict[str, str]:
    token = create_token(user.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def owner(db_session) -> User:
    user = User(email="shopowner@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    identity_repo.grant_role(db_session, user.id, Role.OWNER)
    db_session.flush()
    return user


@pytest.fixture
def student(db_session) -> User:
    user = User(email="student@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


def _kiosk_for(db_session, user: User, name: str):
    kiosk = create_kiosk(db_session, name=name)
    db_session.flush()
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.OWNER)
    )
    set_paper(db_session, kiosk, capacity=250, sheets_left=250, actor_user_id=user.id)
    db_session.flush()
    return kiosk


@pytest.fixture
def kiosk(db_session, owner):
    return _kiosk_for(db_session, owner, "Device Route Shop")


@pytest.fixture
def other_kiosk(db_session, owner):
    return _kiosk_for(db_session, owner, "Device Route Shop Two")


def _enrol(db_session, kiosk) -> str:
    return issue_enrolment_code(db_session, kiosk, created_by_user_id=None).code


def _device_headers(db_session, kiosk) -> dict[str, str]:
    issued = register_device(db_session, _enrol(db_session, kiosk))
    db_session.flush()
    return {"X-Device-Token": issued.token}


def _queue(db_session, store, kiosk, student, *, pages=4, copies=1, duplex=False):
    document = create_document(
        db_session,
        store,
        user_id=student.id,
        filename="lecture-notes.pdf",
        data=make_pdf(pages),
    )
    task = PrintTask(
        document_id=document.id,
        kiosk_id=kiosk.id,
        position=0,
        copies=copies,
        duplex=duplex,
        predicted_sheets=pages * copies,
    )
    db_session.add(task)
    db_session.flush()
    return document, task


# ── enrolment over HTTP ─────────────────────────────────────────────────────


def test_an_owner_enrols_their_kiosk_and_the_agent_registers(
    client, db_session, owner, kiosk
):
    minted = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/enrol", headers=_auth(owner)
    )
    assert minted.status_code == 200, minted.text

    registered = client.post(
        "/v1/device/register",
        json={"enrolment_code": minted.json()["code"], "agent_version": "2.0.0"},
    )

    assert registered.status_code == 200, registered.text
    body = registered.json()
    assert body["kiosk_id"] == kiosk.public_id
    assert body["token"].startswith("dvt_")


def test_registering_without_a_code_is_refused(client):
    response = client.post("/v1/device/register", json={"enrolment_code": "dve_nope"})

    assert response.status_code == 400
    assert "detail" in response.json()


def test_the_enrolment_code_is_never_readable_again(client, db_session, owner, kiosk):
    """It is minted once. There is no endpoint that shows it, because a code
    left readable is a code somebody else can spend."""
    first = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/enrol", headers=_auth(owner)
    ).json()["code"]
    status = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/device", headers=_auth(owner)
    )

    assert first not in status.text


def test_an_owner_sees_their_devices_state(client, db_session, owner, kiosk):
    _device_headers(db_session, kiosk)

    response = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/device", headers=_auth(owner)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["registered"] is True
    # Registered but never heard from: not online, and the owner should be told
    # that rather than shown a hopeful default.
    assert body["online"] is False


def test_revoking_a_device_stops_it_working(client, db_session, owner, kiosk):
    headers = _device_headers(db_session, kiosk)
    assert client.post("/v1/device/heartbeat", json={}, headers=headers).status_code == 200

    client.delete(f"/v1/owner/kiosks/{kiosk.public_id}/device", headers=_auth(owner))

    assert client.post("/v1/device/heartbeat", json={}, headers=headers).status_code == 401


# ── authentication ──────────────────────────────────────────────────────────


def test_a_device_route_without_a_token_is_unauthorised(client):
    assert client.post("/v1/device/heartbeat", json={}).status_code == 401
    assert client.post("/v1/device/tasks/next").status_code == 401


def test_a_students_bearer_token_is_not_a_device_token(client, student):
    """Different credential, different audience. The old backend's Pi routes
    shared a router with student ones."""
    response = client.post("/v1/device/heartbeat", json={}, headers=_auth(student))

    assert response.status_code == 401


# ── heartbeat ───────────────────────────────────────────────────────────────


def test_a_heartbeat_answers_with_what_the_agent_needs_to_decide(
    client, db_session, store, kiosk, student
):
    headers = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student)

    response = client.post(
        "/v1/device/heartbeat", json={"agent_version": "2.0.0"}, headers=headers
    )

    body = response.json()
    assert body["kiosk_id"] == kiosk.public_id
    assert body["queue_depth"] == 1
    assert body["sheets_remaining"] == 250


def test_an_unrecognised_status_is_refused_rather_than_stored(
    client, db_session, kiosk
):
    """The old backend assigned a free-form status straight from the request
    body, and a typo put a kiosk into a state nothing recognised."""
    headers = _device_headers(db_session, kiosk)

    response = client.post(
        "/v1/device/heartbeat", json={"status": "totally-fine"}, headers=headers
    )

    assert response.status_code == 400


# ── claiming ────────────────────────────────────────────────────────────────


def test_claiming_hands_over_a_fully_resolved_task(
    client, db_session, store, kiosk, student
):
    """The agent is told what to do, not asked to work it out from an options
    blob -- which is how the price charged, the paper deducted and the pages
    printed became three separate opinions."""
    headers = _device_headers(db_session, kiosk)
    document, task = _queue(db_session, store, kiosk, student, pages=4, copies=2)

    response = client.post("/v1/device/tasks/next", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == task.public_id
    assert body["document_id"] == document.public_id
    assert body["copies"] == 2
    assert body["expected_sheets"] == 8
    assert body["file_url"].endswith(f"/{task.public_id}/file")
    assert body["lease_expires_at"] is not None


def test_an_empty_queue_answers_null_rather_than_erroring(client, db_session, kiosk):
    headers = _device_headers(db_session, kiosk)

    response = client.post("/v1/device/tasks/next", headers=headers)

    assert response.status_code == 200
    assert response.json() is None


def test_a_second_claim_does_not_get_the_same_task(
    client, db_session, store, kiosk, student
):
    """The duplicate print, at the HTTP layer: the agent's main loop and its
    prefetch worker both asking."""
    headers = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student)

    first = client.post("/v1/device/tasks/next", headers=headers).json()
    second = client.post("/v1/device/tasks/next", headers=headers).json()

    assert first is not None
    assert second is None


def test_one_kiosk_never_claims_anothers_work(
    client, db_session, store, kiosk, other_kiosk, student
):
    theirs = _device_headers(db_session, other_kiosk)
    _queue(db_session, store, kiosk, student)

    assert client.post("/v1/device/tasks/next", headers=theirs).json() is None


# ── the file ────────────────────────────────────────────────────────────────


def test_a_device_downloads_the_file_for_a_task_it_holds(
    client, db_session, store, kiosk, student
):
    headers = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student, pages=3)
    claimed = client.post("/v1/device/tasks/next", headers=headers).json()

    response = client.get(claimed["file_url"], headers=headers)

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")


def test_the_file_is_the_whole_document_never_a_trimmed_copy(
    client, db_session, store, kiosk, student
):
    """The page range goes to CUPS. Trimming server-side as well would apply it
    twice -- asking for pages 5-10 would print pages 5-6 of an already-cut
    file."""
    headers = _device_headers(db_session, kiosk)
    document, task = _queue(db_session, store, kiosk, student, pages=6)
    task.page_range = "2-3"
    db_session.flush()
    claimed = client.post("/v1/device/tasks/next", headers=headers).json()

    response = client.get(claimed["file_url"], headers=headers)

    with pikepdf.open(io.BytesIO(response.content)) as served:
        assert len(served.pages) == 6
    assert claimed["page_range"] == "2-3"


def test_a_device_cannot_download_another_kiosks_file(
    client, db_session, store, kiosk, other_kiosk, student
):
    """This is the old `/pi/jobs/{id}/file`, which any registered Pi could call
    for any job id."""
    mine = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student)
    claimed = client.post("/v1/device/tasks/next", headers=mine).json()

    theirs = _device_headers(db_session, other_kiosk)
    response = client.get(claimed["file_url"], headers=theirs)

    assert response.status_code == 404


def test_a_file_cannot_be_fetched_for_a_task_this_device_does_not_hold(
    client, db_session, store, kiosk, student
):
    headers = _device_headers(db_session, kiosk)
    _document, task = _queue(db_session, store, kiosk, student)

    response = client.get(f"/v1/device/tasks/{task.public_id}/file", headers=headers)

    assert response.status_code == 409


# ── reporting back ──────────────────────────────────────────────────────────


def test_a_finished_print_deducts_the_sheets_the_printer_reported(
    client, db_session, store, kiosk, student
):
    headers = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student, pages=4)
    claimed = client.post("/v1/device/tasks/next", headers=headers).json()

    client.post(
        f"/v1/device/tasks/{claimed['task_id']}/status",
        json={"state": "printed", "sheets_used": 5},
        headers=headers,
    )

    after = client.post("/v1/device/heartbeat", json={}, headers=headers).json()
    assert after["sheets_remaining"] == 245


def test_a_print_that_failed_halfway_still_deducts_what_it_used(
    client, db_session, store, kiosk, student
):
    headers = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student, pages=10)
    claimed = client.post("/v1/device/tasks/next", headers=headers).json()

    client.post(
        f"/v1/device/tasks/{claimed['task_id']}/status",
        json={"state": "failed", "sheets_used": 3, "error_code": "JAM"},
        headers=headers,
    )

    after = client.post("/v1/device/heartbeat", json={}, headers=headers).json()
    assert after["sheets_remaining"] == 247


def test_reporting_the_same_finish_twice_does_not_charge_the_tray_twice(
    client, db_session, store, kiosk, student
):
    """An agent that retries after a network timeout must not empty the tray
    again."""
    headers = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student, pages=4)
    claimed = client.post("/v1/device/tasks/next", headers=headers).json()
    body = {"state": "printed", "sheets_used": 4}

    first = client.post(
        f"/v1/device/tasks/{claimed['task_id']}/status", json=body, headers=headers
    )
    second = client.post(
        f"/v1/device/tasks/{claimed['task_id']}/status", json=body, headers=headers
    )

    assert first.status_code == 200
    assert second.status_code == 409
    after = client.post("/v1/device/heartbeat", json={}, headers=headers).json()
    assert after["sheets_remaining"] == 246


def test_a_device_cannot_report_on_another_kiosks_task(
    client, db_session, store, kiosk, other_kiosk, student
):
    mine = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student)
    claimed = client.post("/v1/device/tasks/next", headers=mine).json()

    theirs = _device_headers(db_session, other_kiosk)
    response = client.post(
        f"/v1/device/tasks/{claimed['task_id']}/status",
        json={"state": "printed", "sheets_used": 4},
        headers=theirs,
    )

    assert response.status_code == 404


def test_a_device_cannot_put_a_task_back_in_the_queue(
    client, db_session, store, kiosk, student
):
    """Requeueing is the lease sweeper's decision, made once with a visible
    attempt count. A device that could requeue its own work could reprint it."""
    headers = _device_headers(db_session, kiosk)
    _queue(db_session, store, kiosk, student)
    claimed = client.post("/v1/device/tasks/next", headers=headers).json()

    response = client.post(
        f"/v1/device/tasks/{claimed['task_id']}/status",
        json={"state": "queued"},
        headers=headers,
    )

    assert response.status_code == 400


def test_a_blocked_task_consumes_no_paper_and_is_not_failed(
    client, db_session, store, kiosk, student
):
    headers = _device_headers(db_session, kiosk)
    _document, task = _queue(db_session, store, kiosk, student, pages=4)
    claimed = client.post("/v1/device/tasks/next", headers=headers).json()

    client.post(
        f"/v1/device/tasks/{claimed['task_id']}/status",
        json={"state": "blocked", "error_code": "NO_PAPER"},
        headers=headers,
    )

    # flush, not refresh: refresh() discards pending changes and re-reads the
    # row, so it would report the state the request had not committed yet.
    db_session.flush()
    assert task.state is TaskState.BLOCKED
    after = client.post("/v1/device/heartbeat", json={}, headers=headers).json()
    assert after["sheets_remaining"] == 250
