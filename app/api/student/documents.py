"""Uploading, listing and removing the files a student wants printed.

Photos and PDFs land in the same place. `/photo-layout` composes the images
into an A4 PDF and then hands it to exactly the same `create_document` a plain
upload uses, so a photo job is validated, page-counted, stored and normalised by
one implementation rather than by a parallel one that can drift.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    get_db,
    get_document_store,
    get_document_use,
    get_settings_from_app,
)
from app.api.schemas import DocumentResponse
from app.core.config import Settings
from app.core.errors import BadRequest
from app.modules.printing import (
    Document,
    DocumentStore,
    DocumentUse,
    create_document,
)
from app.modules.printing import repository as printing_repo
from app.modules.printing.documents import delete_document
from app.modules.printing.photos import parse_layout, render_layout

router = APIRouter(prefix="/v1/app/documents", tags=["student"])

# Read in chunks so a hostile Content-Length cannot make the server allocate
# hundreds of megabytes before the size check runs.
READ_CHUNK = 1024 * 1024


def _as_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        id=document.public_id,
        filename=document.original_filename,
        page_count=document.page_count,
        byte_size=document.byte_size,
        state=document.state.value,
        created_at=document.created_at,
    )


async def _read(upload: UploadFile, *, limit: int) -> bytes:
    """The upload's bytes, refusing to hold more than the cap in memory.

    `inspect_pdf` also checks the length, and that check is the authority. This
    one exists so the refusal happens before the whole file is in memory rather
    than after -- reading 400MB in order to say it is too big is the denial of
    service, not the defence against it.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise BadRequest(
                f"That file is too large. The limit is {limit // (1024 * 1024)} MB."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload(
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> DocumentResponse:
    """Accept a PDF.

    The response carries the page count because that is what the student's next
    decision depends on. It does not carry a price: what a print costs depends
    on which kiosk it goes to and which options are chosen, and that calculation
    belongs to the order, in one place.
    """
    data = await _read(file, limit=settings.max_upload_bytes)

    document = create_document(
        db,
        store,
        user_id=user.id,
        filename=file.filename or "document.pdf",
        data=data,
    )
    return _as_response(document)


@router.post(
    "/photo-layout",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def photo_layout(
    files: Annotated[list[UploadFile], File()],
    layout: Annotated[str, Form()],
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> DocumentResponse:
    """Compose photos onto A4 pages and keep the result as an ordinary document.

    The layout is validated in full before anything is rendered. The old
    implementation skipped elements it could not read, so a student could pay
    for four photos and be handed three with no error anywhere.
    """
    if not files:
        raise BadRequest("No photos were uploaded.")

    images = [
        await _read(upload, limit=settings.max_upload_bytes) for upload in files
    ]

    parsed = parse_layout(layout, image_count=len(images))
    pdf = render_layout(parsed, images)

    document = create_document(
        db,
        store,
        user_id=user.id,
        filename=_photo_filename(files),
        data=pdf,
    )
    return _as_response(document)


@router.get("", response_model=list[DocumentResponse])
def my_documents(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: int = 50,
) -> list[DocumentResponse]:
    return [
        _as_response(document)
        for document in printing_repo.documents_of_user(db, user_id=user.id, limit=limit)
    ]


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove(
    document_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
    in_use: Annotated[DocumentUse, Depends(get_document_use)],
) -> None:
    """Delete one of your own documents.

    Somebody else's is a 404, identical to one that never existed. Refused while
    a job is still waiting to print it, and refused while an order refers to it
    -- tidying a list must not be able to delete the file out from under a print
    that has been paid for.
    """
    document = printing_repo.document_for_user(
        db, user_id=user.id, public_id=document_id
    )
    delete_document(db, store, document, in_use=in_use)


def _photo_filename(files: list[UploadFile]) -> str:
    """Name the PDF after the first photo, so a student recognises it in the
    list. Always `.pdf`, because that is what it now is."""
    first = (files[0].filename or "photos").rsplit("/", 1)[-1]
    stem = first.rsplit(".", 1)[0] or "photos"
    return f"{stem}.pdf"
