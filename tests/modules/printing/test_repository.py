"""Every read names what the row must belong to."""

import pytest

from app.core.errors import NotFound
from app.modules.printing.models import Document, PrintTask
from app.modules.printing.repository import (
    NO_SUCH_TASK,
    document_for_user,
    document_of,
    documents_of_user,
    task_for_kiosk,
)


@pytest.fixture
def user(db_session):
    from app.modules.identity.models import User

    user = User(email="repo@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def other_user(db_session):
    from app.modules.identity.models import User

    user = User(email="stranger@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session):
    from app.modules.kiosks.models import Kiosk

    kiosk = Kiosk(name="Repo Shop")
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def other_kiosk(db_session):
    from app.modules.kiosks.models import Kiosk

    kiosk = Kiosk(name="Repo Shop Two")
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def document(db_session, user):
    doc = Document(user_id=user.id, original_filename="a.pdf", page_count=2)
    db_session.add(doc)
    db_session.flush()
    return doc


@pytest.fixture
def task(db_session, kiosk, document):
    task = PrintTask(
        document_id=document.id, kiosk_id=kiosk.id, position=0, predicted_sheets=2
    )
    db_session.add(task)
    db_session.flush()
    return task


def test_a_kiosk_reads_its_own_task(db_session, kiosk, task):
    assert task_for_kiosk(db_session, kiosk_id=kiosk.id, public_id=task.public_id) is task


def test_another_kiosks_task_is_not_found(db_session, other_kiosk, task):
    with pytest.raises(NotFound):
        task_for_kiosk(db_session, kiosk_id=other_kiosk.id, public_id=task.public_id)


def test_another_kiosks_task_looks_exactly_like_one_that_never_existed(
    db_session, other_kiosk, task
):
    """A different message would confirm that some other shop holds that id."""
    with pytest.raises(NotFound) as theirs:
        task_for_kiosk(db_session, kiosk_id=other_kiosk.id, public_id=task.public_id)
    with pytest.raises(NotFound) as nobodys:
        task_for_kiosk(
            db_session, kiosk_id=other_kiosk.id, public_id="tsk_0000000000000000"
        )

    assert str(theirs.value) == str(nobodys.value) == NO_SUCH_TASK


def test_an_id_of_the_wrong_kind_is_not_found_rather_than_an_error(
    db_session, kiosk, document
):
    """Passing a document id where a task id belongs must not answer differently
    -- and must not raise something the error handler turns into a 500."""
    with pytest.raises(NotFound):
        task_for_kiosk(db_session, kiosk_id=kiosk.id, public_id=document.public_id)


def test_the_document_of_a_task_comes_back(db_session, task, document):
    assert document_of(db_session, task) is document


def test_a_student_reads_their_own_document(db_session, user, document):
    assert (
        document_for_user(db_session, user_id=user.id, public_id=document.public_id)
        is document
    )


def test_someone_elses_document_is_not_found(db_session, other_user, document):
    with pytest.raises(NotFound):
        document_for_user(
            db_session, user_id=other_user.id, public_id=document.public_id
        )


def test_a_students_list_holds_only_their_own(db_session, user, other_user, document):
    db_session.add(Document(user_id=other_user.id, original_filename="theirs.pdf"))
    db_session.flush()

    mine = documents_of_user(db_session, user_id=user.id)

    assert [d.id for d in mine] == [document.id]
