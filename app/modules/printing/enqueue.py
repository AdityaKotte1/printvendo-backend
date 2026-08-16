"""Putting work into a kiosk's queue, and reporting what is already in it.

Both functions exist so that nothing outside this module has to write a
`PrintTask` row. The backend being replaced created and mutated its three core
tables from every router that felt like it, which is how their meanings drifted
apart; here the orders aggregate asks printing to enqueue and printing decides
what a queued task looks like.

`committed_sheets` is the other half of the paper rule. Availability is
**derived** -- sheets in the tray minus sheets already promised to unfinished
tasks -- rather than tracked in a `reserved` column. A counter would need
incrementing on enqueue and decrementing on every one of six terminal paths, and
the one that got missed would drift exactly as the old tray counter did.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.printing.models import TERMINAL_TASK_STATES, PrintTask
from app.modules.printing.options import PrintOptions, workload


def enqueue_task(
    db: Session,
    *,
    document_id: int,
    kiosk_id: int,
    options: PrintOptions,
    total_pages: int,
    position: int = 0,
) -> PrintTask:
    """Queue one document for printing with the settings that were paid for.

    `predicted_sheets` comes from the same `workload()` that priced the order
    and that the device's reported figure will later be compared against. There
    is no second opinion about how much paper this job needs.
    """
    load = workload(options, total_pages=total_pages)

    task = PrintTask(
        document_id=document_id,
        kiosk_id=kiosk_id,
        position=position,
        colour=options.colour,
        duplex=options.duplex,
        copies=options.copies,
        page_range=options.page_range,
        predicted_sheets=load.sheets,
    )
    db.add(task)
    db.flush()
    return task


def committed_sheets(db: Session, *, kiosk_id: int) -> int:
    """Paper already promised to work that has not finished.

    Finished tasks are excluded because the tray has already lost their sheets
    -- the device reported what it used. Counting them here as well would
    reserve the same paper twice, and a kiosk would refuse orders it could
    print perfectly well.

    BLOCKED is *not* finished: it runs as soon as somebody fills the tray, so
    its paper is still spoken for.
    """
    total = db.execute(
        select(func.coalesce(func.sum(PrintTask.predicted_sheets), 0)).where(
            PrintTask.kiosk_id == kiosk_id,
            PrintTask.state.not_in(TERMINAL_TASK_STATES),
        )
    ).scalar_one()
    return int(total)
