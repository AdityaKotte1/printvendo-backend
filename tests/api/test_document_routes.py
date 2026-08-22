"""Student uploads over real HTTP, including photos."""

import io
import json
from datetime import timedelta

import pikepdf
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image

from app.api.deps import get_db, get_document_store, get_notifier, get_secret
from app.core.config import Settings
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity.models import User
from app.modules.kiosks.models import Kiosk
from app.modules.printing.models import Document, PrintTask, TaskState
from app.modules.printing.storage import DocumentStore

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="http://localhost:3000",
)


def make_pdf(pages: int = 2) -> bytes:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def make_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), (10, 120, 200)).save(buffer, format="JPEG")
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
def student(db_session) -> User:
    user = User(email="upload@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def stranger(db_session) -> User:
    user = User(email="stranger-upload@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


# ── uploading a PDF ─────────────────────────────────────────────────────────


def test_a_pdf_upload_comes_back_with_its_page_count(client, student):
    response = client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("notes.pdf", make_pdf(5), "application/pdf")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["page_count"] == 5
    assert body["filename"] == "notes.pdf"
    assert body["id"].startswith("doc_")


def test_the_response_carries_no_price(client, student):
    """What a print costs depends on the kiosk and the options. A price here
    would be a second opinion, and two opinions is how a student is charged for
    one thing and handed another."""
    body = client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("notes.pdf", make_pdf(1), "application/pdf")},
    ).json()

    assert not any("price" in key or "amount" in key for key in body)


def test_uploading_requires_signing_in(client):
    response = client.post(
        "/v1/app/documents",
        files={"file": ("notes.pdf", make_pdf(1), "application/pdf")},
    )

    assert response.status_code == 401


def test_a_file_that_is_not_a_pdf_is_refused_by_its_contents(client, student):
    """Named .pdf, declared application/pdf, and still not a PDF. Both of those
    come from the client; the bytes do not."""
    response = client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("notes.pdf", b"MZ\x90\x00 actually an exe", "application/pdf")},
    )

    assert response.status_code == 400
    assert "not a PDF" in response.json()["detail"]


def test_a_rejected_upload_stores_nothing(client, db_session, store, student):
    client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("x.pdf", b"nope", "application/pdf")},
    )

    assert db_session.query(Document).count() == 0
    assert not list(store.root.rglob("*.pdf"))


def test_a_password_protected_pdf_is_told_what_is_wrong(client, student):
    protected = pikepdf.new()
    protected.add_blank_page(page_size=(595, 842))
    buffer = io.BytesIO()
    protected.save(buffer, encryption=pikepdf.Encryption(owner="p", user="p"))

    response = client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("locked.pdf", buffer.getvalue(), "application/pdf")},
    )

    assert response.status_code == 400
    assert "password" in response.json()["detail"].lower()


# ── photos ──────────────────────────────────────────────────────────────────


def _layout(pages: int = 1, image_index: int = 0) -> str:
    return json.dumps(
        {
            "pages": [
                {
                    "elements": [
                        {
                            "imageIndex": image_index,
                            "x": 0.5,
                            "y": 0.5,
                            "width": 0.8,
                            "height": 0.5,
                            "rotation": 0,
                        }
                    ]
                }
                for _ in range(pages)
            ]
        }
    )


def test_photos_become_an_ordinary_pdf_document(client, student):
    response = client.post(
        "/v1/app/documents/photo-layout",
        headers=_auth(student),
        files=[("files", ("holiday.jpg", make_jpeg(), "image/jpeg"))],
        data={"layout": _layout(pages=2)},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["page_count"] == 2
    assert body["filename"].endswith(".pdf")


def test_a_photo_job_is_stored_like_any_other_document(
    client, db_session, store, student
):
    """One pipeline. After rendering there is nothing special about it, which is
    what stops a photo job drifting away from a document job."""
    body = client.post(
        "/v1/app/documents/photo-layout",
        headers=_auth(student),
        files=[("files", ("holiday.jpg", make_jpeg(), "image/jpeg"))],
        data={"layout": _layout()},
    ).json()

    document = db_session.query(Document).filter_by(public_id=body["id"]).one()
    assert store.read(document.original_path).startswith(b"%PDF-")


def test_a_layout_naming_a_photo_that_was_not_sent_is_refused(client, student):
    """The old implementation skipped it and printed the rest, so a student
    could pay for four photos and be handed three."""
    response = client.post(
        "/v1/app/documents/photo-layout",
        headers=_auth(student),
        files=[("files", ("holiday.jpg", make_jpeg(), "image/jpeg"))],
        data={"layout": _layout(image_index=3)},
    )

    assert response.status_code == 400


def test_an_unreadable_photo_is_refused(client, student):
    response = client.post(
        "/v1/app/documents/photo-layout",
        headers=_auth(student),
        files=[("files", ("holiday.jpg", b"not an image", "image/jpeg"))],
        data={"layout": _layout()},
    )

    assert response.status_code == 400


# ── listing and deleting ────────────────────────────────────────────────────


def test_the_list_holds_only_your_own_documents(client, db_session, student, stranger):
    client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("mine.pdf", make_pdf(1), "application/pdf")},
    )
    client.post(
        "/v1/app/documents",
        headers=_auth(stranger),
        files={"file": ("theirs.pdf", make_pdf(1), "application/pdf")},
    )

    mine = client.get("/v1/app/documents", headers=_auth(student)).json()

    assert [d["filename"] for d in mine] == ["mine.pdf"]


def test_deleting_your_own_document_removes_the_file(
    client, db_session, store, student
):
    body = client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("mine.pdf", make_pdf(1), "application/pdf")},
    ).json()
    document = db_session.query(Document).filter_by(public_id=body["id"]).one()
    key = document.original_path

    response = client.delete(f"/v1/app/documents/{body['id']}", headers=_auth(student))

    assert response.status_code == 204
    assert not store.exists(key)


def test_deleting_someone_elses_document_is_a_404(client, student, stranger):
    body = client.post(
        "/v1/app/documents",
        headers=_auth(stranger),
        files={"file": ("theirs.pdf", make_pdf(1), "application/pdf")},
    ).json()

    response = client.delete(f"/v1/app/documents/{body['id']}", headers=_auth(student))

    assert response.status_code == 404


def test_a_document_waiting_to_print_cannot_be_deleted(
    client, db_session, store, student
):
    """Tidying a list must not delete the file out from under a print that has
    already been paid for."""
    body = client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("mine.pdf", make_pdf(1), "application/pdf")},
    ).json()
    document = db_session.query(Document).filter_by(public_id=body["id"]).one()

    kiosk = Kiosk(name="Delete Guard Shop")
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(
        PrintTask(
            document_id=document.id,
            kiosk_id=kiosk.id,
            position=0,
            predicted_sheets=1,
            state=TaskState.QUEUED,
        )
    )
    db_session.flush()

    response = client.delete(f"/v1/app/documents/{body['id']}", headers=_auth(student))

    assert response.status_code == 409
    assert store.exists(document.original_path)


def test_a_document_whose_print_has_finished_can_be_deleted(
    client, db_session, store, student
):
    body = client.post(
        "/v1/app/documents",
        headers=_auth(student),
        files={"file": ("mine.pdf", make_pdf(1), "application/pdf")},
    ).json()
    document = db_session.query(Document).filter_by(public_id=body["id"]).one()

    kiosk = Kiosk(name="Delete Guard Shop Two")
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(
        PrintTask(
            document_id=document.id,
            kiosk_id=kiosk.id,
            position=0,
            predicted_sheets=1,
            state=TaskState.PRINTED,
        )
    )
    db_session.flush()

    response = client.delete(f"/v1/app/documents/{body['id']}", headers=_auth(student))

    # Refused, and it always was: `print_tasks.document_id` is ON DELETE
    # RESTRICT, so the database rejected this at COMMIT -- after the 204 had
    # been decided and after the file had been removed from the store. The
    # student was told it was gone, the row survived, and the bytes did not.
    assert response.status_code == 409
    assert "record" in response.json()["detail"]
