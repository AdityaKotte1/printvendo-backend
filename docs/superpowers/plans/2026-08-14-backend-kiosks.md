# PrintVendo Backend — Kiosks Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `kiosks` bounded context — the machine registry, the three business types, onboarding, pricing, paper, staffing — and the **scope resolver** that makes "an owner controls only their own kiosks" structural rather than remembered.

**Architecture:** A module under `app/modules/kiosks/` owning `kiosks`, `kiosk_devices`, `kiosk_paper`, `kiosk_assignments`, `paper_refill_logs` and `staff_invites`. Every query that can see a kiosk goes through one scoped repository; there is no unscoped read in route code. This is the first module where authorisation is per-resource rather than per-role, so it is where the pattern for every later module gets set.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres.

**Depends on:** foundation + identity (complete — 209 tests, 5 import contracts)
**Spec:** `docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md` §4, §5, §6

---

## The one idea this module exists to establish

Everything else here is bookkeeping. This is the part that matters:

```python
scope = kiosk_scope(actor)      # OWNER    -> the kiosks they are assigned to
                                # REFILLER -> the kiosks they are assigned to
                                # ADMIN    -> all kiosks
repo.get(scope, kiosk_id)       # 404 for anything outside the scope
repo.list(scope)
```

**Route code cannot obtain an unscoped kiosk query.** Not by convention — the
repository's every read takes a `Scope` as its first argument, and there is no
overload that omits it. An owner asking for someone else's kiosk gets a 404,
because a 403 confirms the kiosk exists.

Admin is not a bypass with its own router (which is what `/owner/*` is today,
carrying a "DO NOT LOOSEN" comment). **Admin is the same resolver returning a
wider scope.** There is no second path to loosen.

---

## Decisions carried in from reading the code being replaced

### D-K1 — Onboarding stages become an enum

`Printer.onboarding_stage` is a free-form `String` today, set from
`payload.onboarding_stage` with no validation (`owner.py:1699`). A typo puts a
kiosk in a stage nothing recognises, and `ONBOARDING_TERMINAL_STAGES` then
silently treats it as stuck. It becomes a real enum with a permitted-transition
table.

### D-K2 — The LIVE gate already exists and is kept verbatim

`owner.py:1676` refuses to set an owned kiosk LIVE unless
`resolves_to_owner_gateway` is true, with a message explaining that the owner's
takings would otherwise land in Printvendo's account. That rule and that message
carry over unchanged. It is D7 from the spec, already implemented.

The new part: the same predicate also *demotes* a LIVE kiosk to
`SUSPENDED_BILLING` when it stops holding, rather than only guarding the
promotion.

### D-K3 — Paper is sheets, and every change is logged

`printer_ops.set_printer_paper` already encodes the rules worth keeping: people
think in sheets remaining while the column stores sheets *used*; changing tray
capacity alone must not add or remove paper; and every change writes a
`PaperRefillLog` row regardless of who made it. All preserved.

Resetting also clears the warning throttle (`paper_warning_count`,
`last_paper_warning_sent_at`, `last_refill_reminder_sent_at`) so the next low-paper
cycle warns again. Miss that and refilling silently disables the alerts.

### D-K4 — `Printer` splits into four tables

One 30-column table today holds business identity, Pi device identity and auth,
pricing, paper counters, and email-nag timestamps. Split by what changes it and
who may change it: `Kiosk`, `KioskDevice`, `KioskPaper`, and the nag state moves
to `ops` later (kept on `KioskPaper` for now rather than inventing a table this
module does not need).

### D-K5 — Staff invites, per spec §6

An owner enters an email. Identical response whether or not an account exists.
No binding, and no name or detail disclosure, until the refiller accepts. This
closes the cross-tenant enumeration and harvesting hole found in
`kiosk.py:911`.

---

## File structure

| Path | Responsibility |
|---|---|
| `app/modules/kiosks/__init__.py` | public surface — services and entity types |
| `app/modules/kiosks/enums.py` | `KioskType`, `OnboardingStage`, `DeviceStatus`, `AssignmentRole` |
| `app/modules/kiosks/models.py` | the six tables |
| `app/modules/kiosks/scope.py` | **`Scope`, `kiosk_scope(actor)`** |
| `app/modules/kiosks/repository.py` | every read/write, all scoped |
| `app/modules/kiosks/registry.py` | create, approve, retire, type changes |
| `app/modules/kiosks/onboarding.py` | stage transitions + the LIVE gate |
| `app/modules/kiosks/pricing.py` | price reads/writes with plan bounds |
| `app/modules/kiosks/paper.py` | reset, set, out-of-paper, refill log |
| `app/modules/kiosks/staffing.py` | invites, accept, unassign |

---

### Task 1: Enums

**Files:**
- Create: `app/modules/kiosks/__init__.py` (empty for now), `app/modules/kiosks/enums.py`
- Test: `tests/modules/kiosks/test_enums.py`

- [ ] **Step 1: Write the failing test**

Create `tests/modules/kiosks/__init__.py` (empty) and `tests/modules/kiosks/test_enums.py`:

```python
import pytest

from app.modules.kiosks.enums import (
    TRANSITIONS,
    AssignmentRole,
    DeviceStatus,
    KioskType,
    OnboardingStage,
    can_transition,
)


def test_kiosk_types_are_the_three_business_relationships():
    assert {t.value for t in KioskType} == {"platform", "sold", "saas"}


def test_assignment_roles_are_owner_and_refiller():
    assert {r.value for r in AssignmentRole} == {"owner", "refiller"}


def test_every_stage_appears_in_the_transition_table():
    assert set(TRANSITIONS) == set(OnboardingStage)


def test_a_new_kiosk_starts_registered():
    assert OnboardingStage.REGISTERED.value == "registered"


def test_the_happy_path_is_permitted():
    assert can_transition(OnboardingStage.REGISTERED, OnboardingStage.APPROVED)
    assert can_transition(OnboardingStage.APPROVED, OnboardingStage.CONFIGURED)
    assert can_transition(OnboardingStage.CONFIGURED, OnboardingStage.LIVE)


def test_a_kiosk_cannot_skip_straight_to_live():
    """Skipping CONFIGURED is how an owned kiosk starts selling before its
    owner's Razorpay account is connected."""
    assert not can_transition(OnboardingStage.REGISTERED, OnboardingStage.LIVE)


def test_live_and_maintenance_are_reversible():
    assert can_transition(OnboardingStage.LIVE, OnboardingStage.MAINTENANCE)
    assert can_transition(OnboardingStage.MAINTENANCE, OnboardingStage.LIVE)


def test_billing_suspension_is_reversible():
    assert can_transition(OnboardingStage.LIVE, OnboardingStage.SUSPENDED_BILLING)
    assert can_transition(OnboardingStage.SUSPENDED_BILLING, OnboardingStage.LIVE)


def test_retired_is_final():
    for stage in OnboardingStage:
        assert not can_transition(OnboardingStage.RETIRED, stage)


def test_a_stage_cannot_transition_to_itself():
    for stage in OnboardingStage:
        assert not can_transition(stage, stage)


@pytest.mark.parametrize("stage", list(OnboardingStage))
def test_every_stage_is_reachable_or_is_the_start(stage):
    """An unreachable stage is dead configuration that will mislead someone."""
    if stage is OnboardingStage.REGISTERED:
        return
    assert any(stage in targets for targets in TRANSITIONS.values()), stage
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/modules/kiosks -v`
Expected: FAIL — no module `app.modules.kiosks.enums`

- [ ] **Step 3: Implement `app/modules/kiosks/enums.py`**

```python
"""Kiosk vocabulary, and the stages a kiosk may move between.

The backend being replaced stored onboarding_stage as a free-form String and
assigned it straight from the request body, so a typo put a kiosk into a stage
nothing recognised -- and the "which kiosks are stuck?" query then silently
treated it as stuck forever. Stages are an enum here, and the moves between them
are a table rather than scattered `if` statements.
"""

from enum import StrEnum


class KioskType(StrEnum):
    """Who installed it, whose Razorpay collects, and what Printvendo earns.

    PLATFORM: we own and run it; the print revenue is ours.
    SOLD:     the shop bought the hardware; their Razorpay; we earn subscription.
    SAAS:     the shop's own printer running our software; their Razorpay.
    """

    PLATFORM = "platform"
    SOLD = "sold"
    SAAS = "saas"


class OnboardingStage(StrEnum):
    REGISTERED = "registered"
    APPROVED = "approved"
    CONFIGURED = "configured"
    LIVE = "live"
    MAINTENANCE = "maintenance"
    SUSPENDED_BILLING = "suspended_billing"
    RETIRED = "retired"


class DeviceStatus(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    PRINTING = "printing"
    ERROR = "error"


class AssignmentRole(StrEnum):
    OWNER = "owner"
    REFILLER = "refiller"


# Which stages a kiosk may move to from each stage.
#
# CONFIGURED cannot be skipped on the way to LIVE: that step is where an owned
# kiosk's Razorpay keys and subscription are confirmed, and skipping it is
# exactly how a shop starts taking student money into the wrong account.
TRANSITIONS: dict[OnboardingStage, set[OnboardingStage]] = {
    OnboardingStage.REGISTERED: {OnboardingStage.APPROVED, OnboardingStage.RETIRED},
    OnboardingStage.APPROVED: {
        OnboardingStage.CONFIGURED,
        OnboardingStage.REGISTERED,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.CONFIGURED: {
        OnboardingStage.LIVE,
        OnboardingStage.APPROVED,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.LIVE: {
        OnboardingStage.MAINTENANCE,
        OnboardingStage.SUSPENDED_BILLING,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.MAINTENANCE: {
        OnboardingStage.LIVE,
        OnboardingStage.SUSPENDED_BILLING,
        OnboardingStage.RETIRED,
    },
    # Re-entered automatically when billing lapses, and left automatically when
    # it is fixed -- see onboarding.reconcile_billing_state.
    OnboardingStage.SUSPENDED_BILLING: {
        OnboardingStage.LIVE,
        OnboardingStage.MAINTENANCE,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.RETIRED: set(),
}


def can_transition(current: OnboardingStage, target: OnboardingStage) -> bool:
    return target in TRANSITIONS[current]
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/kiosks -v     # 17 passed
.venv/Scripts/python -m ruff check .
git add app/modules/kiosks tests/modules/kiosks
git commit -m "feat(kiosks): kiosk vocabulary and onboarding transition table"
```

---

### Task 2: Models

**Files:**
- Create: `app/modules/kiosks/models.py`
- Test: `tests/modules/kiosks/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.ids import IdPrefix, parse_id
from app.modules.identity.models import User
from app.modules.kiosks.enums import (
    AssignmentRole,
    DeviceStatus,
    KioskType,
    OnboardingStage,
)
from app.modules.kiosks.models import (
    Kiosk,
    KioskAssignment,
    KioskDevice,
    KioskPaper,
)


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    k = Kiosk(name="Library Ground Floor")
    db_session.add(k)
    db_session.flush()
    return k


@pytest.fixture
def user(db_session) -> User:
    u = User(email="owner@example.com", hashed_password="x")
    db_session.add(u)
    db_session.flush()
    return u


def test_kiosk_public_id_is_prefixed(db_session, kiosk):
    assert parse_id(kiosk.public_id, IdPrefix.KIOSK)


def test_a_new_kiosk_defaults_to_platform_and_registered(db_session, kiosk):
    assert kiosk.kiosk_type == KioskType.PLATFORM
    assert kiosk.onboarding_stage == OnboardingStage.REGISTERED


def test_a_new_kiosk_does_not_accept_wallet(db_session, kiosk):
    """Wallet top-ups land in the platform's account. Defaulting to True at an
    owner-gateway kiosk would mean the platform keeps the cash while the owner
    prints for free -- wrong permissively costs money, wrong restrictively
    costs a student one payment method."""
    assert kiosk.accepts_wallet is False


def test_kiosk_name_is_unique(db_session, kiosk):
    db_session.add(Kiosk(name=kiosk.name))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_device_public_id_is_prefixed(db_session, kiosk):
    device = KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="h")
    db_session.add(device)
    db_session.flush()
    assert parse_id(device.public_id, IdPrefix.DEVICE)


def test_device_defaults_to_offline(db_session, kiosk):
    device = KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="h")
    db_session.add(device)
    db_session.flush()
    assert device.status == DeviceStatus.OFFLINE


def test_device_key_is_unique(db_session, kiosk):
    db_session.add(KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="a"))
    db_session.flush()
    db_session.add(KioskDevice(kiosk_id=kiosk.id, device_key="pi-001", token_hash="b"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_device_table_stores_no_plaintext_token():
    columns = set(KioskDevice.__table__.columns.keys())
    assert "token_hash" in columns
    assert not {"token", "secret_token", "secret"} & columns


def test_paper_is_one_row_per_kiosk(db_session, kiosk):
    db_session.add(KioskPaper(kiosk_id=kiosk.id))
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_paper_defaults_to_a_full_250_sheet_tray(db_session, kiosk):
    paper = KioskPaper(kiosk_id=kiosk.id)
    db_session.add(paper)
    db_session.flush()
    assert paper.capacity == 250
    assert paper.used == 0


def test_a_user_cannot_hold_the_same_role_at_one_kiosk_twice(db_session, kiosk, user):
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.OWNER)
    )
    db_session.flush()
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.OWNER)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_user_may_be_owner_and_refiller_at_one_kiosk(db_session, kiosk, user):
    """A small shop's owner refills their own paper."""
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.OWNER)
    )
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=AssignmentRole.REFILLER)
    )
    db_session.flush()


def test_legacy_id_exists_for_migration(db_session):
    k = Kiosk(name="Old One", legacy_id=17)
    db_session.add(k)
    db_session.flush()
    assert k.legacy_id == 17
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement `app/modules/kiosks/models.py`**

```python
"""Kiosk tables.

The backend being replaced had one `printers` table of thirty columns holding
four unrelated things: what the business relationship is, how the Pi
authenticates, what a page costs, and how much paper is left. They are split
here by what changes them and who may change them -- a refiller writes paper and
nothing else, and that is far easier to enforce when paper is its own row.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.ids import IdPrefix, new_id
from app.modules.kiosks.enums import (
    AssignmentRole,
    DeviceStatus,
    KioskType,
    OnboardingStage,
)

DEFAULT_PAPER_CAPACITY = 250


class Kiosk(Base):
    __tablename__ = "kiosks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.KIOSK)
    )

    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)

    kiosk_type: Mapped[KioskType] = mapped_column(
        String(20), default=KioskType.PLATFORM, server_default=KioskType.PLATFORM.value
    )
    onboarding_stage: Mapped[OnboardingStage] = mapped_column(
        String(24),
        default=OnboardingStage.REGISTERED,
        server_default=OnboardingStage.REGISTERED.value,
        index=True,
    )
    onboarding_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Whether student wallet balance may be spent here. Top-ups land in the
    # platform's Razorpay, so spending at an owner-gateway kiosk would mean the
    # platform keeps the cash while the owner prints for free. Defaults False:
    # wrong restrictively costs a student one payment method, wrong permissively
    # loses the owner money.
    accepts_wallet: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    location_description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Page prices in rupees. Null means "fall back to the platform default".
    # Bounded by the owner's plan -- see pricing.py.
    price_bw_single: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_bw_double: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_color_single: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_color_double: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    device: Mapped["KioskDevice | None"] = relationship(
        back_populates="kiosk", uselist=False, cascade="all, delete-orphan"
    )
    paper: Mapped["KioskPaper | None"] = relationship(
        back_populates="kiosk", uselist=False, cascade="all, delete-orphan"
    )


class KioskDevice(Base):
    """The Raspberry Pi (or PC) running the agent at a kiosk."""

    __tablename__ = "kiosk_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.DEVICE)
    )

    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), unique=True, index=True
    )

    # The identifier the agent presents. Distinct from public_id: this one is
    # baked into a device's config file and survives re-registration.
    device_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Only ever a hash. A database dump must not yield a token that can pull
    # print jobs.
    token_hash: Mapped[str] = mapped_column(String(64), index=True)

    status: Mapped[DeviceStatus] = mapped_column(
        String(16), default=DeviceStatus.OFFLINE, server_default=DeviceStatus.OFFLINE.value
    )
    capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_update_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    kiosk: Mapped[Kiosk] = relationship(back_populates="device")


class KioskPaper(Base):
    """How much paper is in the tray, and the warning throttle state.

    Stored as sheets *used* against a capacity, because that is what the machine
    reports. Everyone who talks about it thinks in sheets *remaining*, so the
    service layer converts -- see paper.py.
    """

    __tablename__ = "kiosk_paper"

    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), primary_key=True
    )

    capacity: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_PAPER_CAPACITY, server_default=str(DEFAULT_PAPER_CAPACITY)
    )
    used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Throttle state for low-paper email. Cleared on refill, or the next low
    # cycle stays silent.
    warning_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_warning_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    kiosk: Mapped[Kiosk] = relationship(back_populates="paper")


class KioskAssignment(Base):
    """Who is attached to a kiosk, and in what capacity.

    Replaces two structurally identical tables (`printer_owners` and
    `printer_refillers`). A person may legitimately hold both roles at one
    kiosk -- a small shop's owner refills their own paper.
    """

    __tablename__ = "kiosk_assignments"
    __table_args__ = (
        UniqueConstraint("kiosk_id", "user_id", "role", name="uq_kiosk_user_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[AssignmentRole] = mapped_column(String(16), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaperRefillLog(Base):
    """One row per paper change, whoever made it.

    The audit trail has to be complete regardless of whether an owner, a
    refiller or an admin touched the tray -- otherwise "who let this kiosk run
    dry" has no answer.
    """

    __tablename__ = "paper_refill_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    sheets_added: Mapped[int] = mapped_column(Integer)
    capacity_at_change: Mapped[int] = mapped_column(Integer)
    used_before_change: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class StaffInvite(Base):
    """A pending "come and refill my kiosk" invitation.

    An owner names an email address; nothing is bound and no personal detail is
    disclosed until the invitee accepts. That is what closes the cross-tenant
    enumeration hole in the old backend, where an owner could attach any
    refiller in the platform to their kiosk and then read their name and email.
    """

    __tablename__ = "staff_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    invited_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )

    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[AssignmentRole] = mapped_column(String(16))

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/kiosks -v     # 30 passed
git add app/modules/kiosks/models.py tests/modules/kiosks/test_models.py
git commit -m "feat(kiosks): kiosk, device, paper, assignment and invite tables"
```

---

### Task 3: Migration

**Files:**
- Modify: `migrations/env.py` (import the kiosks models)
- Create: `migrations/versions/*_kiosks.py`

- [ ] **Step 1: Register the models**

Add below the identity import in `migrations/env.py`:

```python
from app.modules.kiosks import models as _kiosks_models  # noqa: F401,E402
```

- [ ] **Step 2: Reset the test schema, upgrade, autogenerate**

The `db_session` fixture builds tables with `create_all`, so autogenerate must
run against a schema built only by migrations:

```bash
PGPASSWORD=printvendo "/c/Program Files/PostgreSQL/18/bin/psql.exe" -U printvendo \
  -h 127.0.0.1 -d printvendo_test -c "drop schema public cascade; create schema public;"
```

```powershell
$env:DATABASE_URL = "postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo_test"
.venv\Scripts\alembic upgrade head
.venv\Scripts\alembic revision --autogenerate -m "kiosks"
```

- [ ] **Step 3: Read the generated migration**

Confirm it creates all six tables and carries `uq_kiosk_user_role`, the unique
index on `kiosks.name`, `kiosk_devices.device_key`, `staff_invites.token_hash`,
and the one-to-one constraints on `kiosk_devices.kiosk_id` and
`kiosk_paper.kiosk_id`. Autogenerate misses constraints sometimes; add by hand
if absent.

- [ ] **Step 4: Verify and commit**

```bash
.venv/Scripts/python -m pytest tests/test_migrations.py -v   # 4 passed, no drift
git add migrations
git commit -m "feat(kiosks): create kiosk tables"
```

---

### Task 4: The scope resolver

The centrepiece. Read "The one idea this module exists to establish" above.

**Files:**
- Create: `app/modules/kiosks/scope.py`
- Test: `tests/modules/kiosks/test_scope.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.identity import repository as identity_repo
from app.modules.kiosks.enums import AssignmentRole
from app.modules.kiosks.models import Kiosk, KioskAssignment
from app.modules.kiosks.scope import Scope, kiosk_scope


def _user(db_session, email: str, *roles: Role) -> User:
    user = User(email=email, hashed_password="x")
    db_session.add(user)
    db_session.flush()
    for role in roles:
        identity_repo.grant_role(db_session, user.id, role)
    db_session.flush()
    return user


def _kiosk(db_session, name: str) -> Kiosk:
    kiosk = Kiosk(name=name)
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


def _assign(db_session, kiosk, user, role: AssignmentRole) -> None:
    db_session.add(KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=role))
    db_session.flush()


def test_an_admin_scope_covers_everything(db_session):
    admin = _user(db_session, "admin@example.com", Role.ADMIN)
    scope = kiosk_scope(db_session, admin)
    assert scope.is_unrestricted is True


def test_an_owner_scope_lists_only_their_kiosks(db_session):
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    mine = _kiosk(db_session, "Mine")
    _kiosk(db_session, "Theirs")
    _assign(db_session, mine, owner, AssignmentRole.OWNER)

    scope = kiosk_scope(db_session, owner)
    assert scope.is_unrestricted is False
    assert scope.kiosk_ids == {mine.id}


def test_a_refiller_scope_lists_only_their_kiosks(db_session):
    refiller = _user(db_session, "refiller@example.com", Role.REFILLER)
    mine = _kiosk(db_session, "Mine")
    _kiosk(db_session, "Theirs")
    _assign(db_session, mine, refiller, AssignmentRole.REFILLER)

    assert kiosk_scope(db_session, refiller).kiosk_ids == {mine.id}


def test_a_student_scope_is_empty(db_session):
    """A student has no administrative view of any kiosk. Browsing the public
    list is a different, unauthenticated read."""
    student = _user(db_session, "student@example.com", Role.STUDENT)
    _kiosk(db_session, "Somewhere")
    assert kiosk_scope(db_session, student).kiosk_ids == set()


def test_an_owner_of_two_kiosks_sees_both(db_session):
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    a, b = _kiosk(db_session, "A"), _kiosk(db_session, "B")
    _assign(db_session, a, owner, AssignmentRole.OWNER)
    _assign(db_session, b, owner, AssignmentRole.OWNER)

    assert kiosk_scope(db_session, owner).kiosk_ids == {a.id, b.id}


def test_two_owners_do_not_see_each_others_kiosks(db_session):
    """The whole point of this module."""
    alice = _user(db_session, "alice@example.com", Role.OWNER)
    bob = _user(db_session, "bob@example.com", Role.OWNER)
    a, b = _kiosk(db_session, "Alice Shop"), _kiosk(db_session, "Bob Shop")
    _assign(db_session, a, alice, AssignmentRole.OWNER)
    _assign(db_session, b, bob, AssignmentRole.OWNER)

    assert kiosk_scope(db_session, alice).kiosk_ids == {a.id}
    assert kiosk_scope(db_session, bob).kiosk_ids == {b.id}


def test_admin_scope_wins_even_when_also_an_owner(db_session):
    admin = _user(db_session, "both@example.com", Role.ADMIN, Role.OWNER)
    _kiosk(db_session, "Somewhere")
    assert kiosk_scope(db_session, admin).is_unrestricted is True


def test_scope_allows_checks_a_single_id(db_session):
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    mine, theirs = _kiosk(db_session, "Mine"), _kiosk(db_session, "Theirs")
    _assign(db_session, mine, owner, AssignmentRole.OWNER)

    scope = kiosk_scope(db_session, owner)
    assert scope.allows(mine.id) is True
    assert scope.allows(theirs.id) is False


def test_an_unrestricted_scope_allows_any_id(db_session):
    admin = _user(db_session, "admin@example.com", Role.ADMIN)
    assert kiosk_scope(db_session, admin).allows(9999) is True


def test_an_empty_scope_is_falsy_but_not_unrestricted(db_session):
    """A bug that treats "no kiosks" as "all kiosks" is the worst possible
    failure here, so the two must not be confusable."""
    student = _user(db_session, "student@example.com", Role.STUDENT)
    scope = kiosk_scope(db_session, student)
    assert scope.is_unrestricted is False
    assert scope.allows(1) is False


def test_scope_cannot_be_constructed_unrestricted_by_accident():
    """Scope() with no arguments must not mean "everything"."""
    with pytest.raises(TypeError):
        Scope()
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement `app/modules/kiosks/scope.py`**

```python
"""Which kiosks an actor may act on.

One resolver, used by every kiosk-scoped read and write. The backend being
replaced put admin access in a separate router that filtered by the id in the
URL without checking ownership -- safe only because a dependency restricted it
to admins, and carrying a comment begging nobody to loosen it. Here admin is not
a separate path: it is this function returning an unrestricted scope.

Scope has no default constructor. `Scope()` meaning "everything" is the single
most dangerous typo available in this codebase, so it is not expressible.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.models import KioskAssignment


@dataclass(frozen=True)
class Scope:
    """The set of kiosks an actor may touch.

    `is_unrestricted` is admin. Otherwise `kiosk_ids` is exhaustive -- an empty
    set means no access at all, which is emphatically not the same as full
    access.
    """

    is_unrestricted: bool
    kiosk_ids: frozenset[int]

    def allows(self, kiosk_id: int) -> bool:
        return self.is_unrestricted or kiosk_id in self.kiosk_ids


def kiosk_scope(db: Session, actor: User) -> Scope:
    roles = identity_repo.roles_of(db, actor.id)

    if Role.ADMIN in roles:
        return Scope(is_unrestricted=True, kiosk_ids=frozenset())

    stmt = select(KioskAssignment.kiosk_id).where(KioskAssignment.user_id == actor.id)
    assigned = frozenset(db.execute(stmt).scalars())
    return Scope(is_unrestricted=False, kiosk_ids=assigned)
```

- [ ] **Step 4: Verify the guardrail bites**

Temporarily change `kiosk_scope` to always return `Scope(is_unrestricted=True, ...)`
and confirm `test_two_owners_do_not_see_each_others_kiosks` and
`test_an_empty_scope_is_falsy_but_not_unrestricted` fail. Restore.

- [ ] **Step 5: Commit**

```bash
.venv/Scripts/python -m pytest tests/modules/kiosks/test_scope.py -v   # 11 passed
git add app/modules/kiosks/scope.py tests/modules/kiosks/test_scope.py
git commit -m "feat(kiosks): single scope resolver for kiosk access"
```

---

### Task 5: Scoped repository

**Files:**
- Create: `app/modules/kiosks/repository.py`
- Test: `tests/modules/kiosks/test_repository.py`

Every function takes `Scope` as its first argument. There is no unscoped read.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.core.errors import NotFound
from app.modules.kiosks import repository as repo
from app.modules.kiosks.models import Kiosk
from app.modules.kiosks.scope import Scope


@pytest.fixture
def kiosks(db_session) -> tuple[Kiosk, Kiosk]:
    a, b = Kiosk(name="Alice Shop"), Kiosk(name="Bob Shop")
    db_session.add_all([a, b])
    db_session.flush()
    return a, b


def _only(kiosk: Kiosk) -> Scope:
    return Scope(is_unrestricted=False, kiosk_ids=frozenset({kiosk.id}))


ADMIN = Scope(is_unrestricted=True, kiosk_ids=frozenset())
NOTHING = Scope(is_unrestricted=False, kiosk_ids=frozenset())


def test_list_returns_only_scoped_kiosks(db_session, kiosks):
    a, _ = kiosks
    assert [k.id for k in repo.list_kiosks(db_session, _only(a))] == [a.id]


def test_list_for_an_admin_returns_everything(db_session, kiosks):
    assert len(repo.list_kiosks(db_session, ADMIN)) == 2


def test_list_for_an_empty_scope_returns_nothing(db_session, kiosks):
    assert repo.list_kiosks(db_session, NOTHING) == []


def test_get_returns_a_kiosk_in_scope(db_session, kiosks):
    a, _ = kiosks
    assert repo.get_kiosk(db_session, _only(a), a.public_id).id == a.id


def test_get_raises_not_found_for_a_kiosk_outside_scope(db_session, kiosks):
    """404 rather than 403: a 403 confirms the kiosk exists, which is itself a
    disclosure to someone who has no business knowing."""
    a, b = kiosks
    with pytest.raises(NotFound):
        repo.get_kiosk(db_session, _only(a), b.public_id)


def test_get_raises_not_found_for_an_unknown_id(db_session, kiosks):
    with pytest.raises(NotFound):
        repo.get_kiosk(db_session, ADMIN, "ksk_0000000000000000")


def test_get_raises_not_found_for_an_id_of_the_wrong_kind(db_session, kiosks):
    a, _ = kiosks
    with pytest.raises(NotFound):
        repo.get_kiosk(db_session, ADMIN, a.public_id.replace("ksk_", "usr_"))


def test_the_message_is_identical_inside_and_outside_scope(db_session, kiosks):
    """Otherwise the wording tells an owner whether someone else's kiosk id is
    real."""
    a, b = kiosks

    with pytest.raises(NotFound) as outside:
        repo.get_kiosk(db_session, _only(a), b.public_id)
    with pytest.raises(NotFound) as unknown:
        repo.get_kiosk(db_session, _only(a), "ksk_0000000000000000")

    assert str(outside.value) == str(unknown.value)


def test_inactive_kiosks_are_excluded_by_default(db_session, kiosks):
    a, _ = kiosks
    a.is_active = False
    db_session.flush()
    assert repo.list_kiosks(db_session, ADMIN, include_inactive=False) == [
        k for k in repo.list_kiosks(db_session, ADMIN, include_inactive=True) if k.is_active
    ]


def test_inactive_kiosks_can_be_asked_for_explicitly(db_session, kiosks):
    a, _ = kiosks
    a.is_active = False
    db_session.flush()
    assert len(repo.list_kiosks(db_session, ADMIN, include_inactive=True)) == 2
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement `app/modules/kiosks/repository.py`**

```python
"""Every read and write against the kiosk tables, all scoped.

`Scope` is the first argument of every function here, and there is no overload
that omits it. That is deliberate: it makes "I forgot to filter by owner"
impossible to write rather than merely discouraged.

Anything outside the caller's scope raises NotFound, never Forbidden. A 403
confirms that the kiosk exists, which tells an owner something about a
competitor's estate that they have no business learning.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.kiosks.models import Kiosk
from app.modules.kiosks.scope import Scope

NO_SUCH_KIOSK = "That kiosk does not exist."


def list_kiosks(
    db: Session, scope: Scope, *, include_inactive: bool = False
) -> list[Kiosk]:
    stmt = select(Kiosk)
    if not scope.is_unrestricted:
        stmt = stmt.where(Kiosk.id.in_(scope.kiosk_ids))
    if not include_inactive:
        stmt = stmt.where(Kiosk.is_active.is_(True))
    return list(db.execute(stmt.order_by(Kiosk.name)).scalars())


def get_kiosk(db: Session, scope: Scope, public_id: str) -> Kiosk:
    """The kiosk, or NotFound -- whether it is missing or merely out of scope."""
    try:
        parse_id(public_id, IdPrefix.KIOSK)
    except InvalidId:
        raise NotFound(NO_SUCH_KIOSK) from None

    stmt = select(Kiosk).where(Kiosk.public_id == public_id)
    kiosk = db.execute(stmt).scalar_one_or_none()

    if kiosk is None or not scope.allows(kiosk.id):
        raise NotFound(NO_SUCH_KIOSK)
    return kiosk
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/kiosks/test_repository.py -v   # 10 passed
git add app/modules/kiosks/repository.py tests/modules/kiosks/test_repository.py
git commit -m "feat(kiosks): scoped repository where every read takes a Scope"
```

---

### Task 6: Paper

**Files:**
- Create: `app/modules/kiosks/paper.py`
- Test: `tests/modules/kiosks/test_paper.py`

Behaviour ported from `services/printer_ops.py`, which already encodes the rules
worth keeping (D-K3).

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.core.errors import BadRequest
from app.modules.kiosks.models import Kiosk, KioskPaper, PaperRefillLog
from app.modules.kiosks.paper import (
    mark_out_of_paper,
    reset_paper,
    set_paper,
    sheets_remaining,
)


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    k = Kiosk(name="Shop")
    db_session.add(k)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=k.id, capacity=250, used=200))
    db_session.flush()
    return k


def _logs(db_session, kiosk) -> list[PaperRefillLog]:
    return db_session.query(PaperRefillLog).filter_by(kiosk_id=kiosk.id).all()


def test_sheets_remaining_is_capacity_minus_used(db_session, kiosk):
    assert sheets_remaining(db_session, kiosk) == 50


def test_reset_refills_the_tray(db_session, kiosk):
    reset_paper(db_session, kiosk, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 250


def test_reset_writes_a_log_row(db_session, kiosk):
    reset_paper(db_session, kiosk, actor_user_id=None)
    db_session.flush()
    assert len(_logs(db_session, kiosk)) == 1


def test_reset_clears_the_warning_throttle(db_session, kiosk):
    """Fresh paper must re-arm the alerts. Miss this and refilling silently
    disables low-paper warnings for that kiosk forever."""
    paper = db_session.get(KioskPaper, kiosk.id)
    paper.warning_count = 3
    paper.last_warning_at = paper.last_reminder_at = None
    db_session.flush()

    reset_paper(db_session, kiosk, actor_user_id=None)
    db_session.flush()

    assert paper.warning_count == 0
    assert paper.last_warning_at is None
    assert paper.last_reminder_at is None


def test_set_sheets_left_directly(db_session, kiosk):
    """A refiller rarely fills a tray to the top."""
    set_paper(db_session, kiosk, sheets_left=120, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 120


def test_changing_capacity_alone_preserves_sheets_remaining(db_session, kiosk):
    """Resizing a tray does not add or remove paper."""
    before = sheets_remaining(db_session, kiosk)
    set_paper(db_session, kiosk, capacity=500, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == before


def test_set_with_neither_argument_is_refused(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, actor_user_id=None)


def test_sheets_left_cannot_exceed_capacity(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, sheets_left=9999, actor_user_id=None)


def test_sheets_left_cannot_be_negative(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, sheets_left=-1, actor_user_id=None)


def test_capacity_must_be_positive(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, capacity=0, actor_user_id=None)


def test_mark_out_of_paper_empties_the_tray(db_session, kiosk):
    mark_out_of_paper(db_session, kiosk, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 0


def test_every_change_is_logged(db_session, kiosk):
    reset_paper(db_session, kiosk, actor_user_id=None)
    set_paper(db_session, kiosk, sheets_left=10, actor_user_id=None)
    mark_out_of_paper(db_session, kiosk, actor_user_id=None)
    db_session.flush()

    assert len(_logs(db_session, kiosk)) == 3


def test_a_kiosk_with_no_paper_row_gets_one(db_session):
    k = Kiosk(name="Fresh")
    db_session.add(k)
    db_session.flush()

    assert sheets_remaining(db_session, k) == 250
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement `app/modules/kiosks/paper.py`**

```python
"""Paper: how much is in the tray, and who changed it.

Two rules that are not preferences:

* **Paper is sheets, never a percentage.** Nobody refills a percentage. Both the
  tray size and the amount in it are editable, because a tray is not always 250
  sheets and a refiller rarely fills it to the top.
* **Every change writes a log row**, whoever made it. Otherwise "who let this
  kiosk run dry" has no answer.

Storage is sheets *used* against a capacity, because that is what the machine
reports; every function here speaks in sheets *remaining*, because that is what
people mean.
"""

from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.modules.kiosks.models import (
    DEFAULT_PAPER_CAPACITY,
    Kiosk,
    KioskPaper,
    PaperRefillLog,
)


def _paper(db: Session, kiosk: Kiosk) -> KioskPaper:
    paper = db.get(KioskPaper, kiosk.id)
    if paper is None:
        paper = KioskPaper(kiosk_id=kiosk.id, capacity=DEFAULT_PAPER_CAPACITY, used=0)
        db.add(paper)
        db.flush()
    return paper


def sheets_remaining(db: Session, kiosk: Kiosk) -> int:
    paper = _paper(db, kiosk)
    return max(0, paper.capacity - paper.used)


def _log(
    db: Session,
    kiosk: Kiosk,
    paper: KioskPaper,
    *,
    sheets_added: int,
    used_before: int,
    actor_user_id: int | None,
    note: str | None,
) -> None:
    db.add(
        PaperRefillLog(
            kiosk_id=kiosk.id,
            actor_user_id=actor_user_id,
            sheets_added=sheets_added,
            capacity_at_change=paper.capacity,
            used_before_change=used_before,
            note=note,
        )
    )


def reset_paper(db: Session, kiosk: Kiosk, *, actor_user_id: int | None) -> int:
    """Refill to a full tray. Returns sheets remaining."""
    paper = _paper(db, kiosk)
    used_before = paper.used

    paper.used = 0

    # Fresh paper re-arms the alerts. Without this the next low-paper cycle
    # stays silent because the throttle still thinks it has warned.
    paper.warning_count = 0
    paper.last_warning_at = None
    paper.last_reminder_at = None

    _log(
        db,
        kiosk,
        paper,
        sheets_added=used_before,
        used_before=used_before,
        actor_user_id=actor_user_id,
        note="reset to full",
    )
    db.add(paper)
    return paper.capacity


def set_paper(
    db: Session,
    kiosk: Kiosk,
    *,
    capacity: int | None = None,
    sheets_left: int | None = None,
    actor_user_id: int | None = None,
    note: str | None = None,
) -> int:
    """Set tray size, amount in it, or both. Returns sheets remaining.

    Changing capacity alone preserves sheets remaining -- resizing a tray does
    not add or remove paper.
    """
    if capacity is None and sheets_left is None:
        raise BadRequest("Say how big the tray is, how much paper is in it, or both.")

    paper = _paper(db, kiosk)
    used_before = paper.used
    old_left = max(0, paper.capacity - paper.used)

    if capacity is not None:
        if capacity < 1:
            raise BadRequest("A paper tray holds at least one sheet.")
        paper.capacity = capacity

    new_left = old_left if sheets_left is None else sheets_left
    if new_left < 0:
        raise BadRequest("A tray cannot hold a negative number of sheets.")
    if new_left > paper.capacity:
        raise BadRequest(
            f"That tray holds {paper.capacity} sheets, so it cannot contain {new_left}."
        )

    paper.used = paper.capacity - new_left

    if new_left > old_left:
        paper.warning_count = 0
        paper.last_warning_at = None
        paper.last_reminder_at = None

    _log(
        db,
        kiosk,
        paper,
        sheets_added=max(0, new_left - old_left),
        used_before=used_before,
        actor_user_id=actor_user_id,
        note=note,
    )
    db.add(paper)
    return new_left


def mark_out_of_paper(db: Session, kiosk: Kiosk, *, actor_user_id: int | None) -> int:
    """The tray is empty -- reported by the device or by a person."""
    return set_paper(
        db, kiosk, sheets_left=0, actor_user_id=actor_user_id, note="reported empty"
    )
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/kiosks/test_paper.py -v   # 13 passed
git add app/modules/kiosks/paper.py tests/modules/kiosks/test_paper.py
git commit -m "feat(kiosks): paper in sheets, with a complete change log"
```

---

## Remaining tasks

Tasks 7-12 follow the same shape and are written out when Task 6 lands, so their
tests can be written against the interfaces that actually exist rather than
guessed ones:

7. **Registry** — create, approve, retire, change type. Approval is admin-only.
8. **Onboarding** — stage transitions through `can_transition`, the LIVE gate
   (D-K2), and `reconcile_billing_state` which demotes a LIVE kiosk to
   `SUSPENDED_BILLING` when its owner's billing lapses. The gate itself calls
   into `payments` and so is stubbed behind a protocol until sub-project 5.
9. **Pricing** — read/write with plan floor and ceiling enforcement. The band
   comes from the plan and is returned alongside the prices, so no client keeps
   a second copy to drift.
10. **Staffing** — invite by email, accept, revoke, unassign (D-K5). Identical
    response whether or not the address has an account.
11. **Owner + refiller API layers** — `/v1/owner/kiosks/*`, `/v1/refiller/kiosks/*`,
    with matrix entries added first.
12. **Module surface, independence contract, docs.**

---

## Done when

- An owner sees and can act on only their own kiosks; a second owner's kiosk is
  a 404, with the same message as a kiosk that does not exist
- Admin sees everything through the same resolver, not a separate router
- A refiller can change paper and read refill logs, and can reach nothing else
- Onboarding stages are an enum with validated transitions; LIVE is unreachable
  for an owned kiosk whose billing is not connected
- Paper is sheets, capacity and fill are separately editable, and every change
  is logged
- An owner cannot discover or bind another shop's refiller
- Every new route is in `tests/authz/matrix.py`
- `lint-imports` reports 6 contracts kept (kiosks added to the independence list)
