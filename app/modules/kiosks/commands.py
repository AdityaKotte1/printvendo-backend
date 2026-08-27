"""Asking the machine in a shop to do something, and hearing back.

Two things live here because they are the same subject — the health of the
machine at a kiosk — and the backend being replaced had them in three places:
`/kiosk/printers/{id}/restart` for an owner, a second copy in `pi.py` for an
admin, and a third path that set a printer's status from a device report
without going through the stage rules at all.

**A command is a row, claimed over HTTP.** The socket carries a wake and never
work, exactly as it does for print tasks. A restart pushed down a connection
that drops mid-flight is a restart nobody can tell happened.

**A command expires.** A restart asked for at four o'clock and run when the
machine reconnects at five restarts a shop that has been printing again for an
hour, in front of a customer, for a reason that resolved itself. Print tasks are
owed to somebody who paid and are retried for ever; a command is a request about
*now*.

**Asking twice does not queue twice.** An operator who clicks a button that
appears to do nothing clicks it again — that is not a mistake, it is what a
button that appears to do nothing is for. A second identical request that is
still waiting returns the first one.

**A stuck printer closes the shop, and only reopens one it closed.**
`report_stuck` moves the kiosk to MAINTENANCE, which `is_selling` already
excludes, so the student app stops offering it while the owner console still
shows everything. `stuck_since` on the device row is what says *we* are the
reason: an owner who put their own shop into maintenance to change a cartridge
must not have it reopened by a queue clearing.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.bus import mark_for_wake
from app.core.errors import BadRequest, NotFound
from app.modules.kiosks.enums import (
    DeviceCommandKind,
    DeviceCommandState,
    OnboardingStage,
)
from app.modules.kiosks.models import Kiosk, KioskDevice, KioskDeviceCommand
from app.modules.kiosks.onboarding import BillingCheck, move_to

# How long a command is worth running. Longer than one poll interval by a wide
# margin, so an ordinary slow round trip is never mistaken for a machine that
# is not listening; short enough that a restart is about the situation the
# person was looking at.
COMMAND_LIFETIME = timedelta(minutes=10)

# The states a command can still change from.
OPEN_STATES = (DeviceCommandState.QUEUED, DeviceCommandState.SENT)

NO_DEVICE = (
    "No machine is enrolled at this kiosk, so there is nothing to restart. "
    "Enrol one first."
)
NO_SUCH_COMMAND = "That command does not exist."
ALREADY_FINISHED = "That command has already finished."

STUCK_NOTE = (
    "Closed automatically: the machine at this kiosk could not finish a print "
    "job. It will reopen on its own once printing works again."
)


def request_command(
    db: Session,
    kiosk: Kiosk,
    kind: DeviceCommandKind,
    *,
    requested_by_user_id: int | None = None,
    now: datetime | None = None,
) -> KioskDeviceCommand:
    """Ask this kiosk's machine to do something.

    Refuses when there is no machine: a queued restart for a kiosk nobody has
    enrolled would sit there until it expired, and the operator would be told
    their request went through.
    """
    now = now or datetime.now(UTC)

    device = db.execute(
        select(KioskDevice).where(KioskDevice.kiosk_id == kiosk.id)
    ).scalar_one_or_none()
    if device is None:
        raise BadRequest(NO_DEVICE)

    waiting = db.execute(
        select(KioskDeviceCommand)
        .where(
            KioskDeviceCommand.kiosk_id == kiosk.id,
            KioskDeviceCommand.kind == kind,
            KioskDeviceCommand.state.in_(OPEN_STATES),
            KioskDeviceCommand.expires_at > now,
        )
        .order_by(KioskDeviceCommand.id.desc())
    ).scalars().first()
    if waiting is not None:
        return waiting

    command = KioskDeviceCommand(
        kiosk_id=kiosk.id,
        requested_by_user_id=requested_by_user_id,
        kind=kind,
        state=DeviceCommandState.QUEUED,
        expires_at=now + COMMAND_LIFETIME,
    )
    db.add(command)
    db.flush()

    # Marked here rather than by the route, for the reason every other wake is:
    # a route that has to remember is a route that can forget, and the one
    # nobody added looks exactly like a decision. `get_db` sends it after the
    # commit -- a machine woken before then would ask, see nothing, and have
    # spent its notification.
    mark_for_wake(db, kiosk.id)
    return command


def claim_commands(
    db: Session, device: KioskDevice, *, now: datetime | None = None
) -> list[KioskDeviceCommand]:
    """Hand this machine everything waiting for it, and mark it sent.

    Everything, not one: an operator who asked for both restarts wants both,
    and a machine that took one per pass would apply the second fifteen seconds
    later — after the first restart had already killed the loop that would have
    fetched it.

    Expired commands are marked as expired here rather than by a sweep. The
    device asking is the only moment the answer matters, and a row nobody ever
    reads does not need a background job to tidy it.
    """
    now = now or datetime.now(UTC)

    rows = db.execute(
        select(KioskDeviceCommand)
        .where(
            KioskDeviceCommand.kiosk_id == device.kiosk_id,
            KioskDeviceCommand.state == DeviceCommandState.QUEUED,
        )
        .order_by(KioskDeviceCommand.id)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    claimed = []
    for command in rows:
        if command.expires_at <= now:
            command.state = DeviceCommandState.EXPIRED
            command.finished_at = now
        else:
            command.state = DeviceCommandState.SENT
            command.sent_at = now
            claimed.append(command)
        db.add(command)

    db.flush()
    return claimed


def report_command(
    db: Session,
    device: KioskDevice,
    public_id: str,
    *,
    succeeded: bool,
    error_message: str | None = None,
    now: datetime | None = None,
) -> KioskDeviceCommand:
    """Record how a command went.

    Scoped to the device that was given it. A command belonging to another
    kiosk is 404 and not 403, for the same reason a kiosk outside somebody's
    scope is: the answer must not confirm that the other one exists.
    """
    now = now or datetime.now(UTC)

    command = db.execute(
        select(KioskDeviceCommand).where(
            KioskDeviceCommand.public_id == public_id,
            KioskDeviceCommand.kiosk_id == device.kiosk_id,
        )
    ).scalar_one_or_none()
    if command is None:
        raise NotFound(NO_SUCH_COMMAND)

    if command.state not in OPEN_STATES:
        raise BadRequest(ALREADY_FINISHED)

    command.state = (
        DeviceCommandState.SUCCEEDED if succeeded else DeviceCommandState.FAILED
    )
    command.error_message = None if succeeded else error_message
    command.finished_at = now
    db.add(command)
    db.flush()
    return command


def recent_commands(
    db: Session, kiosk: Kiosk, *, limit: int = 20
) -> list[KioskDeviceCommand]:
    """The last few, newest first, so an operator can see whether it worked."""
    return list(
        db.execute(
            select(KioskDeviceCommand)
            .where(KioskDeviceCommand.kiosk_id == kiosk.id)
            .order_by(KioskDeviceCommand.id.desc())
            .limit(limit)
        ).scalars()
    )


# ── the printer being stuck ─────────────────────────────────────────────────


def report_stuck(
    db: Session,
    device: KioskDevice,
    kiosk: Kiosk,
    *,
    billing: BillingCheck,
    now: datetime | None = None,
) -> bool:
    """The machine cannot get a job out of the printer. Close the shop.

    Returns whether this call is what closed it, so the caller can decide
    whether to raise an alert — a machine that keeps saying so every minute
    must not fill the console with the same shop.

    Only a LIVE kiosk is closed. One already in MAINTENANCE is where somebody
    put it, and one that is SUSPENDED_BILLING has a bigger problem than a
    paper jam.

    **`stuck_since` is written only when we actually close the shop**, and the
    order of these checks is the whole of that guarantee. It used to be set
    before the stage was looked at, so a jam at a shop an owner had already put
    into maintenance recorded us as the reason it was shut -- and the next
    recovery then handed that shop back to students with the printer in pieces
    on the counter. The field means "we did this"; anything else makes
    `report_recovered` reopen a door it never closed.
    """
    now = now or datetime.now(UTC)

    if device.stuck_since is not None:
        return False

    if kiosk.onboarding_stage is not OnboardingStage.LIVE:
        # Nothing recorded. We are not why this shop is shut, and saying we are
        # is how a person's decision gets undone by a queue clearing.
        return False

    device.stuck_since = now
    db.add(device)

    move_to(db, kiosk, OnboardingStage.MAINTENANCE, billing=billing, note=STUCK_NOTE)
    return True


def report_recovered(
    db: Session, device: KioskDevice, kiosk: Kiosk, *, billing: BillingCheck
) -> bool:
    """Printing works again. Reopen the shop, if we are why it is shut.

    Returns whether this call reopened it. `stuck_since` is the whole test: a
    kiosk in maintenance with nothing recorded here was put there by a person,
    and a person is who takes it out again. `report_stuck` only writes that
    field when it actually closes the shop, which is what makes the test mean
    what it says.

    **The claim is released unconditionally, before the stage is looked at, and
    that is deliberate.** If an admin has already moved the kiosk out of
    MAINTENANCE, there is nothing here to reopen -- but the machine is working
    again, so we no longer hold a claim on it either. Gating the clear on the
    stage would leave `stuck_since` set on a shop somebody else had already
    reopened, and the next time an *owner* closed that shop for a cartridge, a
    recovery would hand it back under them. That is the bug this field exists
    to prevent, arrived at from the other direction.
    """
    if device.stuck_since is None:
        return False

    device.stuck_since = None
    db.add(device)

    if kiosk.onboarding_stage is not OnboardingStage.MAINTENANCE:
        return False

    move_to(db, kiosk, OnboardingStage.LIVE, billing=billing, note=None)
    kiosk.onboarding_note = None
    db.add(kiosk)
    return True
