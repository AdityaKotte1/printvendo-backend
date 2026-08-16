"""The printing bounded context.

Documents students upload, the options they chose, and the tasks that put them
on paper exactly once.

Import from here, never from the submodules' internals. Entity types are part of
the contract because callers must annotate what services return; the *tables*
are not, and importing `app.modules.printing.models` directly from the api layer
breaks the import contracts.

`PaperLedger` is deliberately exported without an implementation. Paper belongs
to the kiosks context, so this module says what it needs and the composition
root supplies it -- rather than the two contexts importing each other.
"""

from app.modules.printing.claims import (
    LEASE,
    MAX_ATTEMPTS,
    claim_next_task,
    queue_depth,
    renew_lease,
    requeue_expired,
)
from app.modules.printing.documents import (
    create_document,
    normalise_document,
    printable_key,
    purge_expired_files,
)
from app.modules.printing.models import (
    TERMINAL_TASK_STATES,
    Document,
    DocumentState,
    PrintTask,
    TaskState,
)
from app.modules.printing.options import (
    MAX_COPIES,
    PrintOptions,
    Workload,
    format_page_range,
    parse_page_range,
    workload,
)
from app.modules.printing.pdfs import PdfFacts, inspect_pdf
from app.modules.printing.storage import DocumentStore, StorageArea
from app.modules.printing.tasks import (
    PaperLedger,
    report_blocked,
    report_failed,
    report_printed,
    start_printing,
)

__all__ = [
    "LEASE",
    "MAX_ATTEMPTS",
    "MAX_COPIES",
    "TERMINAL_TASK_STATES",
    "Document",
    "DocumentState",
    "DocumentStore",
    "PaperLedger",
    "PdfFacts",
    "PrintOptions",
    "PrintTask",
    "StorageArea",
    "TaskState",
    "Workload",
    "claim_next_task",
    "create_document",
    "format_page_range",
    "inspect_pdf",
    "normalise_document",
    "parse_page_range",
    "printable_key",
    "purge_expired_files",
    "queue_depth",
    "renew_lease",
    "report_blocked",
    "report_failed",
    "report_printed",
    "requeue_expired",
    "start_printing",
    "workload",
]
