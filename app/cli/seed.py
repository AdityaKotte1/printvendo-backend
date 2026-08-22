"""A world an operator can click through, built out of the ordinary services.

Nothing here writes a row directly. The kiosk is created by `create_kiosk` and
climbs the onboarding ladder; its staff are *invited* and accept; its paper goes
through the paper log; the student's balance is a ledger credit. A seed that
inserted rows would produce a world the product itself cannot produce, and the
first defect it found would be its own.

**It refuses to run in production**, for two independent reasons: a demo kiosk
is a fake shop in every student's list, and at cutover it is a row the
migration's reconciliation cannot account for -- and reconciliation balancing is
the only evidence anyone will have that the migration was correct.

The kiosk is PLATFORM. That is not laziness: a SOLD kiosk cannot reach LIVE
without an owner who can actually collect, which means real Razorpay keys, and
requiring those to click through a print would make the first end-to-end test
wait for an account that has nothing to do with printing.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.cli.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.core.errors import BadRequest, Conflict
from app.modules.identity import Role, User, register
from app.modules.identity import repository as identity_repo
from app.modules.kiosks import (
    AssignmentRole,
    Kiosk,
    KioskType,
    OnboardingStage,
    PlatformBand,
    accept_invite,
    create_kiosk,
    invite_staff,
    issue_enrolment_code,
    move_to,
    set_accepts_wallet,
    set_paper,
    set_pricing,
)
from app.modules.payments.gate import GateBilling
from app.modules.wallet import EntryKind, credit

NOT_IN_PRODUCTION = (
    "Demo data must not be created in production: it would appear to students "
    "as a real shop, and at cutover it is a row the migration's reconciliation "
    "cannot account for."
)

DEFAULT_NAME = "Demo Print Shop"
DOMAIN = "demo.printvendo.com"

TRAY = 500
STARTING_BALANCE = Decimal("500.00")

# Per **sheet**: `_single` is a sheet printed on one side, `_double` a sheet
# printed on both. Black and white works out at 1.50 a page double-sided
# against 2.00 single-sided; colour is 10.00 a page either way. Pricing refuses
# a double rate below the single one, and the first version of this seed was
# refused for getting that backwards.
PRICES = {
    "bw_single": Decimal("2.00"),
    "bw_double": Decimal("3.00"),
    "color_single": Decimal("10.00"),
    "color_double": Decimal("20.00"),
}


@dataclass(frozen=True)
class SeededWorld:
    """Everything the operator needs to start clicking, in one object."""

    kiosk: Kiosk
    admin: User
    owner: User
    refiller: User
    student: User
    # Generated, never fixed. A seed with a hardcoded password is a seed
    # somebody eventually runs on staging and forgets about.
    passwords: dict[str, str]
    enrolment_code: str
    enrolment_expires_at: datetime


def seed_demo(
    db: Session, settings: Settings, *, name: str = DEFAULT_NAME
) -> SeededWorld:
    if settings.ENV == "prod":
        raise BadRequest(NOT_IN_PRODUCTION)

    passwords: dict[str, str] = {}

    # The shop first, so a second run collides on the kiosk name -- which has a
    # sentence saying what to do -- rather than on an email address, which does
    # not, and which would leave four accounts behind before it failed.
    kiosk = _kiosk(db, name)
    slug = _slug(name)

    # `force` because a demo world wants its own admin whether or not one
    # exists, and the audit trail records that it was made from the command
    # line. The guard it steps around protects a production system, and this
    # command has already refused to run on one.
    admin = _person(db, "admin", slug, passwords, force_admin=True)
    owner = _person(db, "owner", slug, passwords, role=Role.OWNER)
    refiller = _person(db, "refiller", slug, passwords, role=Role.REFILLER)
    student = _person(db, "student", slug, passwords)

    # Invited and accepted rather than assigned, so the seeded world is one the
    # consent flow could have produced -- and so the invitation machinery is
    # exercised on every seed rather than first meeting a real shop.
    _attach(db, kiosk, owner, AssignmentRole.OWNER, invited_by=admin)
    _attach(db, kiosk, refiller, AssignmentRole.REFILLER, invited_by=owner)

    credit(
        db,
        user_id=student.id,
        amount=STARTING_BALANCE,
        kind=EntryKind.TOPUP,
        reference=f"seed:{secrets.token_hex(8)}",
        note="seeded starting balance",
    )

    enrolment = issue_enrolment_code(db, kiosk, created_by_user_id=admin.id)

    return SeededWorld(
        kiosk=kiosk,
        admin=admin,
        owner=owner,
        refiller=refiller,
        student=student,
        passwords=passwords,
        enrolment_code=enrolment.code,
        enrolment_expires_at=enrolment.expires_at,
    )


def _slug(name: str) -> str:
    """A kiosk name as an email local part, so two seeded worlds can coexist.

    Without it the second `--name` still collides, on `owner@…` rather than on
    the shop -- which reads as a bug in the command rather than as a world that
    is already there.
    """
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in name)
    return "-".join(part for part in cleaned.split("-") if part)


def _person(
    db: Session,
    who: str,
    slug: str,
    passwords: dict[str, str],
    *,
    role: Role | None = None,
    force_admin: bool = False,
) -> User:
    email = f"{who}.{slug}@{DOMAIN}"
    password = secrets.token_urlsafe(12)
    passwords[email] = password

    if force_admin:
        return bootstrap_admin(
            db, email=email, password=password, full_name="Demo Admin", force=True
        )

    user = register(db, email, password, f"Demo {who.title()}")
    if role is not None:
        identity_repo.grant_role(db, user.id, role)
    return user


def _kiosk(db: Session, name: str) -> Kiosk:
    try:
        kiosk = create_kiosk(
            db,
            name=name,
            kiosk_type=KioskType.PLATFORM,
            location_description="Seeded for testing",
            # Roughly a campus in Bengaluru. Coordinates at all, because the
            # student app sorts shops by distance and a list of one shop at
            # (0, 0) is a thousand kilometres out to sea.
            latitude=12.9716,
            longitude=77.5946,
            paper_capacity=TRAY,
        )
    except Conflict as clash:
        raise Conflict(
            f"{clash.detail} Seed a second one with --name, or drop and "
            f"recreate the database if this one has served its purpose."
        ) from clash

    # The ladder, one rung at a time. CONFIGURED cannot be skipped, and LIVE is
    # refused for a kiosk that cannot take a payment -- both of which is the
    # point of seeding through the services rather than around them.
    billing = GateBilling()
    move_to(db, kiosk, OnboardingStage.APPROVED, billing=billing)

    # `PlatformBand` is unbounded, which is what the real band source also
    # answers for a platform kiosk. The bounded case exists to stop an *owner*
    # charging beyond the plan they are paying for, and no owner is doing this.
    set_pricing(db, kiosk, bands=PlatformBand(), **PRICES)

    move_to(db, kiosk, OnboardingStage.CONFIGURED, billing=billing)
    move_to(db, kiosk, OnboardingStage.LIVE, billing=billing)

    set_paper(db, kiosk, capacity=TRAY, sheets_left=TRAY, note="seeded full tray")

    # Without this the first end-to-end run stops at "pay by card or UPI": a new
    # kiosk does not accept wallet money until somebody says so. Legal here
    # because a PLATFORM kiosk collects into the account the balance is held in.
    set_accepts_wallet(db, kiosk, accepts_wallet=True)
    return kiosk


def _attach(
    db: Session, kiosk: Kiosk, user: User, role: AssignmentRole, *, invited_by: User
) -> None:
    token = invite_staff(
        db, kiosk, email=user.email, role=role, invited_by_user_id=invited_by.id
    )
    accept_invite(db, token, user=user)
