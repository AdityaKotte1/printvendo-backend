# Backend Foundation & Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close two live production bugs (wallet money leak, student PII exposure) and add the schema plus paper-control endpoints the new owner app and admin console depend on.

**Architecture:** Purely additive changes to `cloud-backend`. New nullable columns on `printers`, a resolver that decides whether a kiosk may accept wallet money, server-side enforcement of that rule, removal of student contact fields from the owner-facing job feed, and new endpoints to set paper capacity and sheets remaining. The existing admin dashboard and student app keep working throughout — nothing is renamed or removed.

**Tech Stack:** FastAPI, SQLAlchemy, Pytest, Postgres (prod) / SQLite (tests). Migrations are standalone `migrate_*.py` scripts run manually, matching the existing convention — this repo does not use Alembic revisions.

**Spec:** `docs/superpowers/specs/2026-08-10-admin-redesign-design.md`

**Not in this plan** (separate plans follow): subscription plans tables, pricing limits, audit log, onboarding pipeline, owner refunds, queue/search endpoints, and both new frontend apps.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/models/printer.py` | MODIFY — four new columns |
| `app/services/wallet_eligibility.py` | CREATE — the single rule deciding if a kiosk may take wallet money |
| `app/routers/wallet.py` | MODIFY — enforce that rule on both hold endpoints |
| `app/routers/kiosk.py` | MODIFY — drop PII from job feed; add paper endpoint |
| `app/routers/refiller.py` | MODIFY — add paper endpoint |
| `app/services/printer_ops.py` | MODIFY — `set_printer_paper()` alongside the existing reset |
| `app/schemas/paper.py` | CREATE — request bodies for the paper endpoints |
| `migrate_add_kiosk_fields.py` | CREATE — the additive migration |
| `backfill_accepts_wallet.py` | CREATE — one-off, idempotent, restrictive by default |
| `tests/test_wallet_eligibility.py` | CREATE |
| `tests/test_owner_job_privacy.py` | CREATE |
| `tests/test_paper_controls.py` | CREATE |

`wallet_eligibility.py` is a service, not a router helper, because `wallet.py`, `payments.py` and the backfill script all need the same answer. One rule, one place.

---

### Task 1: New columns on `printers`

**Files:**
- Modify: `cloud-backend/app/models/printer.py`
- Test: `cloud-backend/tests/test_model_registry.py` (existing — must keep passing)

- [ ] **Step 1: Add the columns to the model**

In `app/models/printer.py`, after the `is_approved` line:

```python
    # ── kiosk classification (see docs/superpowers/specs/2026-08-10-admin-redesign-design.md) ──
    # PLATFORM: we own and run it, revenue is ours.
    # SOLD:     shop bought the hardware, their Razorpay, we earn subscription.
    # SAAS:     shop's own PC running our agent, their Razorpay, subscription.
    kiosk_type = Column(String, default="PLATFORM", nullable=False)

    # Whether student wallet balance may be spent here. Wallet top-ups land in
    # the PLATFORM Razorpay account, so spending at an owner-key kiosk would
    # mean we keep the cash while the owner prints for free. Defaults False:
    # wrong restrictively costs a student one payment method, wrong
    # permissively loses the owner money.
    accepts_wallet = Column(Boolean, default=False, nullable=False)

    # Where this kiosk is in getting live. Free text note says why it is stuck.
    onboarding_stage = Column(String, default="REGISTERED", nullable=False)
    onboarding_note = Column(String, nullable=True)
```

- [ ] **Step 2: Run the existing suite to prove nothing broke**

Run: `cd cloud-backend && python -m pytest -q`
Expected: `170 passed` (the SQLite test DB is created from the models, so the new columns appear automatically)

- [ ] **Step 3: Commit**

```bash
git add app/models/printer.py
git commit -m "feat(printers): add kiosk_type, accepts_wallet, onboarding fields"
```

---

### Task 2: The migration script

**Files:**
- Create: `cloud-backend/migrate_add_kiosk_fields.py`

- [ ] **Step 1: Write the migration**

Follow the existing `migrate_add_refiller_role.py` pattern exactly.

```python
"""Add kiosk classification and wallet-eligibility columns to printers.

Run manually (matches the migrate_*.py convention):

    python migrate_add_kiosk_fields.py

Purely additive and idempotent. Old code keeps working against the new
schema, so the running API does not need to be stopped first and a rollback
of the application needs no down-migration.

accepts_wallet defaults to FALSE on purpose. Run backfill_accepts_wallet.py
afterwards to switch it on for platform kiosks only.
"""
from sqlalchemy import text

from app.db.session import engine

COLUMNS_PG = [
    ("kiosk_type", "VARCHAR NOT NULL DEFAULT 'PLATFORM'"),
    ("accepts_wallet", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("onboarding_stage", "VARCHAR NOT NULL DEFAULT 'REGISTERED'"),
    ("onboarding_note", "VARCHAR"),
]

COLUMNS_SQLITE = [
    ("kiosk_type", "VARCHAR NOT NULL DEFAULT 'PLATFORM'"),
    ("accepts_wallet", "BOOLEAN NOT NULL DEFAULT 0"),
    ("onboarding_stage", "VARCHAR NOT NULL DEFAULT 'REGISTERED'"),
    ("onboarding_note", "VARCHAR"),
]


def run() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            for name, ddl in COLUMNS_PG:
                conn.execute(
                    text(f"ALTER TABLE printers ADD COLUMN IF NOT EXISTS {name} {ddl}")
                )
        else:
            # SQLite ADD COLUMN is not conditional; swallow the duplicate.
            for name, ddl in COLUMNS_SQLITE:
                try:
                    conn.execute(text(f"ALTER TABLE printers ADD COLUMN {name} {ddl}"))
                except Exception as exc:  # noqa: BLE001
                    if "duplicate column" not in str(exc).lower():
                        raise
    print("printers: kiosk_type, accepts_wallet, onboarding_stage, onboarding_note ensured")


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Test it against a throwaway SQLite database, twice**

```bash
cd cloud-backend
rm -f /tmp/mig.db
DATABASE_URL="sqlite:////tmp/mig.db" python -c "
from app.db.session import Base, engine
import app.models  # noqa
Base.metadata.create_all(engine)
print('schema created')
"
DATABASE_URL="sqlite:////tmp/mig.db" python migrate_add_kiosk_fields.py
DATABASE_URL="sqlite:////tmp/mig.db" python migrate_add_kiosk_fields.py
```

Expected: the confirmation line printed twice, no traceback on the second run.

- [ ] **Step 3: Commit**

```bash
git add migrate_add_kiosk_fields.py
git commit -m "feat(migration): add kiosk classification columns"
```

---

### Task 3: The wallet-eligibility rule

**Files:**
- Create: `cloud-backend/app/services/wallet_eligibility.py`
- Test: `cloud-backend/tests/test_wallet_eligibility.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_wallet_eligibility.py`:

```python
"""Wallet money may only be spent where the money already is.

Top-ups land in the platform Razorpay account. If a student spends that
balance at a kiosk whose owner uses their own keys, the platform keeps the
cash and the owner prints for free. These tests pin that rule.
"""
from app.models.kiosk_payment_config import KioskPaymentConfig
from app.services.wallet_eligibility import kiosk_accepts_wallet
from tests.conftest import make_printer, make_user, own


def test_unowned_printer_accepts_wallet(db):
    printer = make_printer(db)
    assert kiosk_accepts_wallet(db, printer) is True


def test_owner_without_payment_config_accepts_wallet(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    assert kiosk_accepts_wallet(db, printer) is True


def test_owner_with_configured_keys_rejects_wallet(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    db.add(
        KioskPaymentConfig(
            user_id=owner.id,
            razorpay_key_id="rzp_test_owner",
            razorpay_key_secret="secret",
            is_configured=True,
        )
    )
    db.commit()
    assert kiosk_accepts_wallet(db, printer) is False


def test_owner_with_unconfigured_config_row_accepts_wallet(db):
    """A row that exists but is not marked configured is not a live gateway."""
    printer = make_printer(db)
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    db.add(KioskPaymentConfig(user_id=owner.id, is_configured=False))
    db.commit()
    assert kiosk_accepts_wallet(db, printer) is True
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd cloud-backend && python -m pytest tests/test_wallet_eligibility.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.wallet_eligibility'`

- [ ] **Step 3: Write the service**

Create `app/services/wallet_eligibility.py`:

```python
"""Decides whether student wallet balance may be spent at a kiosk.

Wallet top-ups are collected into the PLATFORM Razorpay account. Payments at
a kiosk whose owner has configured their own keys go straight to that owner
(see payments.get_razorpay_client_for_printer). Allowing wallet spend there
would mean the platform holds money the owner earned, with no settlement to
return it — the owner prints for free.

Lives in a neutral service module so wallet.py, payments.py and the backfill
script all reach the same answer. One rule, one place.
"""
from sqlalchemy.orm import Session

from app.models.kiosk_payment_config import KioskPaymentConfig
from app.models.printer import Printer
from app.models.printer_owner import PrinterOwner


def kiosk_accepts_wallet(db: Session, printer: Printer) -> bool:
    """True when payments at this kiosk land in the platform's account."""
    ownership = (
        db.query(PrinterOwner).filter(PrinterOwner.printer_id == printer.id).first()
    )
    if ownership is None:
        # Unowned kiosk — platform keys are used, so wallet is spendable.
        return True

    config = (
        db.query(KioskPaymentConfig)
        .filter(
            KioskPaymentConfig.user_id == ownership.user_id,
            KioskPaymentConfig.is_configured == True,  # noqa: E712
        )
        .first()
    )
    if config and config.razorpay_key_id and config.razorpay_key_secret:
        return False

    # Owner exists but has no live gateway: the platform still collects, so
    # wallet works and the owner is paid through settlements.
    return True
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `cd cloud-backend && python -m pytest tests/test_wallet_eligibility.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add app/services/wallet_eligibility.py tests/test_wallet_eligibility.py
git commit -m "feat(wallet): add kiosk wallet-eligibility rule"
```

---

### Task 4: Enforce the rule on both wallet hold endpoints (bug B1)

**Files:**
- Modify: `cloud-backend/app/routers/wallet.py:366` and `:509`
- Test: `cloud-backend/tests/test_wallet_eligibility.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wallet_eligibility.py`:

```python
import pytest
from fastapi import HTTPException

from app.routers.wallet import _assert_wallet_allowed


def test_assert_rejects_owner_key_kiosk(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko2@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    db.add(
        KioskPaymentConfig(
            user_id=owner.id,
            razorpay_key_id="rzp_test_owner",
            razorpay_key_secret="secret",
            is_configured=True,
        )
    )
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _assert_wallet_allowed(db, printer)
    assert exc.value.status_code == 400
    assert "wallet" in str(exc.value.detail).lower()


def test_assert_allows_platform_kiosk(db):
    printer = make_printer(db)
    assert _assert_wallet_allowed(db, printer) is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd cloud-backend && python -m pytest tests/test_wallet_eligibility.py -q`
Expected: FAIL — `ImportError: cannot import name '_assert_wallet_allowed'`

- [ ] **Step 3: Add the guard and call it from both endpoints**

In `app/routers/wallet.py`, add near the other imports:

```python
from app.services.wallet_eligibility import kiosk_accepts_wallet
```

Add this helper above `hold_wallet_amount_for_job`:

```python
def _assert_wallet_allowed(db: Session, printer: Printer) -> None:
    """Reject wallet payment at a kiosk that collects into its own account.

    The UI hides the wallet option at these kiosks, but the UI is convenience
    and this is the guarantee — two students paying at the same moment must
    both be stopped here, not in a browser.
    """
    if not kiosk_accepts_wallet(db, printer):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This shop does not accept wallet payments. Please pay by UPI or card.",
        )
```

In `hold_wallet_amount_for_job`, immediately after line 366 (`printer = _get_available_printer_or_404(...)`):

```python
    _assert_wallet_allowed(db, printer)
```

In `hold_wallet_amount_for_multiple_jobs`, immediately after line 509 (`printer = _get_available_printer_or_404(...)`):

```python
    _assert_wallet_allowed(db, printer)
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `cd cloud-backend && python -m pytest tests/test_wallet_eligibility.py -q`
Expected: `6 passed`

- [ ] **Step 5: Run the whole suite**

Run: `cd cloud-backend && python -m pytest -q`
Expected: `176 passed`

- [ ] **Step 6: Commit**

```bash
git add app/routers/wallet.py tests/test_wallet_eligibility.py
git commit -m "fix(wallet): reject wallet payment at owner-key kiosks

Wallet top-ups are collected by the platform. Spending that balance at a
kiosk whose owner uses their own Razorpay keys meant the platform kept the
money while the owner printed for free."
```

---

### Task 5: Expose `accepts_wallet` on the printer list

**Files:**
- Modify: `cloud-backend/app/routers/printers.py`
- Test: `cloud-backend/tests/test_printers_list.py` (existing — extend)

- [ ] **Step 1: Find the serializer**

Run: `cd cloud-backend && grep -n '"is_favorite"' app/routers/printers.py`
Expected: one or more lines inside the printer list response builder. Note the line number; the next step adds a key beside it.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_printers_list.py`:

```python
def test_printer_payload_exposes_accepts_wallet(db):
    """The student app needs this to hide the wallet option."""
    from app.models.printer import Printer

    printer = make_printer(db, printer_id="PW", name="Wallet Test")
    fetched = db.query(Printer).filter(Printer.id == printer.id).one()
    assert hasattr(fetched, "accepts_wallet")
    assert fetched.accepts_wallet is False
```

- [ ] **Step 3: Run it**

Run: `cd cloud-backend && python -m pytest tests/test_printers_list.py -q`
Expected: PASS (the column exists from Task 1). If it fails with `AttributeError`, Task 1 was not applied.

- [ ] **Step 4: Add the field to the list response**

In `app/routers/printers.py`, in the dict built for each printer in the public list endpoint, add beside `"is_favorite"`:

```python
                "accepts_wallet": bool(p.accepts_wallet),
```

- [ ] **Step 5: Run the whole suite**

Run: `cd cloud-backend && python -m pytest -q`
Expected: `177 passed`

- [ ] **Step 6: Commit**

```bash
git add app/routers/printers.py tests/test_printers_list.py
git commit -m "feat(printers): expose accepts_wallet to clients"
```

---

### Task 6: Remove student PII from the owner job feed (bug B2)

**Files:**
- Modify: `cloud-backend/app/routers/kiosk.py` (the dict at roughly lines 1060-1075)
- Test: `cloud-backend/tests/test_owner_job_privacy.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_owner_job_privacy.py`:

```python
"""A shop needs to identify a document, never a person.

The owner-facing job feed previously returned the student's email and phone
alongside the filename, so a shop could see that a named person printed a
named file. Filename stays — the counter needs it to hand over the right
print. Identity goes.
"""
import inspect

from app.routers import kiosk

FORBIDDEN = ("user_email", "user_phone")


def test_owner_job_feed_source_has_no_contact_fields():
    source = inspect.getsource(kiosk.kiosk_printer_jobs)
    for field in FORBIDDEN:
        assert f'"{field}"' not in source, f"{field} must not be sent to kiosk owners"


def test_owner_job_feed_still_returns_filename():
    source = inspect.getsource(kiosk.kiosk_printer_jobs)
    assert '"filename"' in source, "the counter needs the filename to hand over a print"
```

- [ ] **Step 2: Confirm the function name, then run the test**

Run: `cd cloud-backend && grep -n "def kiosk_printer_jobs" app/routers/kiosk.py`
Expected: one match. If the function has a different name, use that name in the test instead.

Run: `cd cloud-backend && python -m pytest tests/test_owner_job_privacy.py -q`
Expected: FAIL — `user_email must not be sent to kiosk owners`

- [ ] **Step 3: Delete the three fields**

In `app/routers/kiosk.py`, in the job dict, delete these three lines exactly:

```python
                "user_id": j.job.user_id if j.job else None,
                "user_email": j.job.user.email if j.job and j.job.user else None,
                "user_phone": getattr(j.job.user, 'phone', None) if j.job and j.job.user else None,
```

Leave `"filename"` and every other key untouched.

- [ ] **Step 4: Run the tests**

Run: `cd cloud-backend && python -m pytest tests/test_owner_job_privacy.py -q`
Expected: `2 passed`

- [ ] **Step 5: Check nothing else read those fields**

Run: `cd cloud-backend && grep -rn "user_email\|user_phone" ../printit-admin-dashboard/*.html | head`
Expected: no matches inside the printer-detail job table. If there are matches, the old dashboard renders them; they will simply render blank after this change, which is acceptable — that dashboard is being retired. Note any matches in the commit body.

- [ ] **Step 6: Run the whole suite and commit**

Run: `cd cloud-backend && python -m pytest -q`
Expected: `179 passed`

```bash
git add app/routers/kiosk.py tests/test_owner_job_privacy.py
git commit -m "fix(privacy): stop sending student email and phone to kiosk owners"
```

---

### Task 7: `set_printer_paper` service

**Files:**
- Modify: `cloud-backend/app/services/printer_ops.py`
- Test: `cloud-backend/tests/test_paper_controls.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_paper_controls.py`:

```python
"""Paper is a number, not a button.

paper_capacity and paper_used existed but nothing could set them: the only
moves were reset (full, assuming 250) and out-of-paper (empty). A tray is not
always 250 sheets and a refiller rarely fills it exactly to the top.
"""
import pytest

from app.models.paper_refill_log import PaperRefillLog
from app.services.printer_ops import set_printer_paper
from tests.conftest import make_printer, make_user


def test_set_sheets_left_updates_used(db):
    printer = make_printer(db, paper_capacity=250, paper_used=200)
    user = make_user(db, email="owner@test.in")

    set_printer_paper(db, printer, sheets_left=120, actor_user_id=user.id)

    db.refresh(printer)
    assert printer.paper_capacity == 250
    assert printer.paper_used == 130  # 250 - 120


def test_set_capacity_only_keeps_sheets_left(db):
    """Changing the tray size must not silently change how much is in it."""
    printer = make_printer(db, paper_capacity=250, paper_used=150)  # 100 left
    user = make_user(db, email="owner@test.in")

    set_printer_paper(db, printer, capacity=300, actor_user_id=user.id)

    db.refresh(printer)
    assert printer.paper_capacity == 300
    assert printer.paper_used == 200  # still 100 left


def test_sheets_left_clamped_to_capacity(db):
    printer = make_printer(db, paper_capacity=100, paper_used=0)
    user = make_user(db, email="owner@test.in")

    set_printer_paper(db, printer, sheets_left=500, actor_user_id=user.id)

    db.refresh(printer)
    assert printer.paper_used == 0  # clamped to full, never negative


def test_every_change_is_logged(db):
    printer = make_printer(db, paper_capacity=250, paper_used=200)
    user = make_user(db, email="owner@test.in")

    set_printer_paper(db, printer, sheets_left=120, actor_user_id=user.id)

    log = db.query(PaperRefillLog).filter(PaperRefillLog.printer_id == printer.id).one()
    assert log.refilled_by_user_id == user.id
    assert log.used_before_refill == 200
    assert log.capacity_at_refill == 250


def test_rejects_a_call_that_changes_nothing(db):
    printer = make_printer(db)
    user = make_user(db, email="owner@test.in")
    with pytest.raises(ValueError):
        set_printer_paper(db, printer, actor_user_id=user.id)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd cloud-backend && python -m pytest tests/test_paper_controls.py -q`
Expected: FAIL — `ImportError: cannot import name 'set_printer_paper'`

- [ ] **Step 3: Write the service**

Append to `app/services/printer_ops.py`:

```python
def set_printer_paper(
    db: Session,
    printer: Printer,
    *,
    capacity: int | None = None,
    sheets_left: int | None = None,
    actor_user_id: int | None = None,
    note: str | None = None,
) -> int:
    """Set the tray size and/or how much paper is actually in it.

    Stored as `paper_used`, but people think in sheets remaining, so that is
    what callers pass. Changing capacity alone preserves sheets remaining —
    resizing a tray does not add or remove paper.

    Writes a PaperRefillLog row for every change so the audit trail matches
    the existing reset/out-of-paper flow. Commits. Returns sheets remaining.
    """
    if capacity is None and sheets_left is None:
        raise ValueError("Pass capacity, sheets_left, or both")

    old_capacity = printer.paper_capacity or DEFAULT_PAPER_CAPACITY
    old_used = printer.paper_used or 0
    old_left = max(0, old_capacity - old_used)

    new_capacity = old_capacity if capacity is None else max(1, int(capacity))
    new_left = old_left if sheets_left is None else max(0, int(sheets_left))
    new_left = min(new_left, new_capacity)

    printer.paper_capacity = new_capacity
    printer.paper_used = new_capacity - new_left

    db.add(
        PaperRefillLog(
            printer_id=printer.id,
            refilled_by_user_id=actor_user_id,
            sheets_added=max(0, new_left - old_left),
            capacity_at_refill=old_capacity,
            used_before_refill=old_used,
            note=note,
            refilled_at=datetime.utcnow(),
        )
    )

    # Fresh paper clears the warning throttle, same as a full reset does.
    if new_left > old_left:
        printer.paper_warning_count = 0
        printer.last_paper_warning_sent_at = None
        printer.last_refill_reminder_sent_at = None

    db.commit()
    db.refresh(printer)
    return new_left
```

- [ ] **Step 4: Run the tests**

Run: `cd cloud-backend && python -m pytest tests/test_paper_controls.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add app/services/printer_ops.py tests/test_paper_controls.py
git commit -m "feat(paper): set tray capacity and sheets remaining directly"
```

---

### Task 8: Paper endpoints for owner and refiller

**Files:**
- Create: `cloud-backend/app/schemas/paper.py`
- Modify: `cloud-backend/app/routers/kiosk.py`, `cloud-backend/app/routers/refiller.py`
- Test: `cloud-backend/tests/test_paper_controls.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paper_controls.py`:

```python
from app.routers.kiosk import kiosk_set_paper
from app.routers.refiller import refiller_set_paper
from app.schemas.paper import PaperUpdate
from app.models.printer_refiller import PrinterRefiller
from tests.conftest import own


def test_owner_sets_capacity_and_sheets(db):
    printer = make_printer(db, paper_capacity=250, paper_used=250)
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    own(db, owner, printer)

    out = kiosk_set_paper(
        printer_id=printer.id,
        payload=PaperUpdate(capacity=100, sheets_left=40),
        db=db,
        current_user=owner,
    )

    assert out["capacity"] == 100
    assert out["sheets_left"] == 40


def test_owner_cannot_set_paper_on_someone_elses_kiosk(db):
    from fastapi import HTTPException

    printer = make_printer(db)
    stranger = make_user(db, email="other@test.in", is_kiosk_owner=True)

    with pytest.raises(HTTPException) as exc:
        kiosk_set_paper(
            printer_id=printer.id,
            payload=PaperUpdate(sheets_left=10),
            db=db,
            current_user=stranger,
        )
    assert exc.value.status_code == 403


def test_refiller_sets_sheets_on_assigned_kiosk(db):
    printer = make_printer(db, paper_capacity=250, paper_used=250)
    refiller = make_user(db, email="rf@test.in", is_refiller=True)
    db.add(PrinterRefiller(user_id=refiller.id, printer_id=printer.id))
    db.commit()

    out = refiller_set_paper(
        printer_id=printer.id,
        payload=PaperUpdate(sheets_left=120),
        db=db,
        current_user=refiller,
    )

    assert out["sheets_left"] == 120


def test_refiller_cannot_set_capacity(db):
    """Tray size is an owner decision; a refiller reports what they loaded."""
    from fastapi import HTTPException

    printer = make_printer(db)
    refiller = make_user(db, email="rf2@test.in", is_refiller=True)
    db.add(PrinterRefiller(user_id=refiller.id, printer_id=printer.id))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        refiller_set_paper(
            printer_id=printer.id,
            payload=PaperUpdate(capacity=500),
            db=db,
            current_user=refiller,
        )
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd cloud-backend && python -m pytest tests/test_paper_controls.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.paper'`

- [ ] **Step 3: Write the schema**

Create `app/schemas/paper.py`:

```python
from pydantic import BaseModel, Field


class PaperUpdate(BaseModel):
    """Set tray size and/or how much paper is in it.

    Both optional so a caller can change one without touching the other, but
    the service rejects a call that changes neither.
    """

    capacity: int | None = Field(default=None, ge=1, le=5000)
    sheets_left: int | None = Field(default=None, ge=0, le=5000)
    note: str | None = Field(default=None, max_length=200)
```

- [ ] **Step 4: Add the owner endpoint**

In `app/routers/kiosk.py`, add the imports:

```python
from app.schemas.paper import PaperUpdate
from app.services.printer_ops import set_printer_paper
```

(`set_printer_paper` joins the existing `from app.services.printer_ops import fail_active_jobs, reset_printer_paper` line.)

Add the endpoint next to the existing paper routes:

```python
@router.put("/kiosk/printers/{printer_id}/paper")
def kiosk_set_paper(
    printer_id: int,
    payload: PaperUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> dict[str, Any]:
    """Set tray capacity and/or sheets remaining.

    The existing reset and out-of-paper endpoints remain as shortcuts.
    """
    printer = _assert_owns_printer(db, current_user, printer_id)
    try:
        sheets_left = set_printer_paper(
            db,
            printer,
            capacity=payload.capacity,
            sheets_left=payload.sheets_left,
            actor_user_id=current_user.id,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return {
        "status": "ok",
        "printer_id": printer.id,
        "capacity": printer.paper_capacity,
        "sheets_left": sheets_left,
    }
```

- [ ] **Step 5: Add the refiller endpoint**

In `app/routers/refiller.py`, add the imports:

```python
from app.schemas.paper import PaperUpdate
from app.services.printer_ops import set_printer_paper
```

(`set_printer_paper` joins the existing `from app.services.printer_ops import reset_printer_paper` line.)

Add the endpoint:

```python
@router.put("/refiller/printers/{printer_id}/paper")
def refiller_set_paper(
    printer_id: int,
    payload: PaperUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_refiller_user),
) -> dict[str, Any]:
    """Report how many sheets are actually in the tray after a refill.

    Capacity is deliberately refused: tray size is an owner decision, and a
    refiller reporting "I loaded 120" must not silently resize the tray.
    """
    if payload.capacity is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the shop owner can change the tray size.",
        )

    printer = _assert_services_printer(db, current_user, printer_id)
    try:
        sheets_left = set_printer_paper(
            db,
            printer,
            sheets_left=payload.sheets_left,
            actor_user_id=current_user.id,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return {
        "status": "ok",
        "printer_id": printer.id,
        "capacity": printer.paper_capacity,
        "sheets_left": sheets_left,
    }
```

- [ ] **Step 6: Run the tests**

Run: `cd cloud-backend && python -m pytest tests/test_paper_controls.py -q`
Expected: `9 passed`

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd cloud-backend && python -m pytest -q`
Expected: `188 passed`

```bash
git add app/schemas/paper.py app/routers/kiosk.py app/routers/refiller.py tests/test_paper_controls.py
git commit -m "feat(paper): owner and refiller endpoints to set capacity and sheets"
```

---

### Task 9: Backfill `accepts_wallet`

**Files:**
- Create: `cloud-backend/backfill_accepts_wallet.py`

- [ ] **Step 1: Write the script**

```python
"""Set accepts_wallet on existing printers.

Runs after migrate_add_kiosk_fields.py, which defaults every row to FALSE.
This switches it TRUE only where payments demonstrably resolve to the
platform's own Razorpay account, using the same rule the API enforces.

Idempotent — safe to re-run after an owner configures or removes their keys.
Also seeds kiosk_type as a starting guess: unowned kiosks are PLATFORM,
owned ones are SOLD. That guess is corrected by hand in the console; nothing
depends on it being right today.
"""
from app.db.session import SessionLocal
from app.models.printer import Printer
from app.services.wallet_eligibility import kiosk_accepts_wallet


def run() -> None:
    db = SessionLocal()
    try:
        printers = db.query(Printer).all()
        wallet_on = 0
        for printer in printers:
            allowed = kiosk_accepts_wallet(db, printer)
            printer.accepts_wallet = allowed
            if allowed:
                wallet_on += 1
            printer.kiosk_type = "PLATFORM" if allowed else "SOLD"
        db.commit()
        print(f"{len(printers)} printers processed")
        print(f"  accepts_wallet=True : {wallet_on}")
        print(f"  accepts_wallet=False: {len(printers) - wallet_on}")
        print("kiosk_type seeded as a guess — correct it in the console.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Verify against a throwaway database**

```bash
cd cloud-backend
rm -f /tmp/bf.db
DATABASE_URL="sqlite:////tmp/bf.db" python -c "
from app.db.session import Base, engine, SessionLocal
import app.models  # noqa
from app.models.printer import Printer
from app.models.user import User
from app.models.printer_owner import PrinterOwner
from app.models.kiosk_payment_config import KioskPaymentConfig
Base.metadata.create_all(engine)
db = SessionLocal()
platform = Printer(printer_id='P1', name='Platform', secret_token_hash='x')
owned = Printer(printer_id='P2', name='Owned', secret_token_hash='y')
db.add_all([platform, owned]); db.commit()
owner = User(email='o@test.in', hashed_password='x', is_kiosk_owner=True)
db.add(owner); db.commit()
db.add(PrinterOwner(user_id=owner.id, printer_id=owned.id))
db.add(KioskPaymentConfig(user_id=owner.id, razorpay_key_id='k', razorpay_key_secret='s', is_configured=True))
db.commit(); db.close(); print('seeded 2 printers')
"
DATABASE_URL="sqlite:////tmp/bf.db" python backfill_accepts_wallet.py
```

Expected:
```
2 printers processed
  accepts_wallet=True : 1
  accepts_wallet=False: 1
```

- [ ] **Step 3: Commit**

```bash
git add backfill_accepts_wallet.py
git commit -m "feat(migration): backfill accepts_wallet and seed kiosk_type"
```

---

### Task 10: Tidy the misleading admin dependency

**Files:**
- Modify: `cloud-backend/app/routers/wallet.py:99` and `:187`

Not a vulnerability — both endpoints already check `is_admin` in the body (`wallet.py:102`). The dependency simply says something untrue, which is how a real hole gets introduced later.

- [ ] **Step 1: Swap the dependency**

In `app/routers/wallet.py`, add to the imports:

```python
from app.core.security import get_current_admin_user
```

In `admin_wallet_refund` and `admin_wallet_bank_refund`, change:

```python
    current_user: User = Depends(get_non_guest_user),
```

to:

```python
    current_user: User = Depends(get_current_admin_user),
```

Leave the in-body `is_admin` check in place — defence in depth, and removing it while changing the dependency would make a mistake silent.

- [ ] **Step 2: Run the whole suite**

Run: `cd cloud-backend && python -m pytest -q`
Expected: `188 passed`

- [ ] **Step 3: Commit**

```bash
git add app/routers/wallet.py
git commit -m "refactor(wallet): admin refunds declare the admin dependency they require"
```

---

### Task 11: Student app — hide wallet where it is not accepted

**Files:**
- Modify: `printvendo-web/lib/types.ts`, `printvendo-web/components/flow/PayStep.tsx`, `printvendo-web/lib/printerState.ts`

- [ ] **Step 1: Add the field to the Printer type**

In `printvendo-web/lib/types.ts`, inside `export type Printer`, add:

```typescript
  accepts_wallet?: boolean;
```

Optional, so a backend that has not deployed yet does not break the build.

- [ ] **Step 2: Add the maintenance state**

In `printvendo-web/lib/printerState.ts`, change the type and the switch:

```typescript
export type PrinterAvailability = 'ready' | 'busy' | 'maintenance' | 'error' | 'offline';
```

Add to the switch in `availabilityOf`, before `default`:

```typescript
    case 'MAINTENANCE':
      return 'maintenance';
```

Add to `availabilityLabel`, before `default`:

```typescript
    case 'maintenance':
      return 'Under maintenance';
```

`canAcceptJobs` already returns true only for `'ready'`, so a kiosk in maintenance correctly refuses jobs — this only fixes the label, which previously read "Offline" and made a shop look broken while its owner changed the toner.

- [ ] **Step 3: Hide the wallet method**

In `printvendo-web/components/flow/PayStep.tsx`, change the `walletUsable` line:

```typescript
  // Wallet top-ups are collected by Printvendo, so the balance can only be
  // spent at Printvendo-run kiosks. Hidden silently elsewhere; the Wallet
  // page explains where a balance can be used.
  const shopTakesWallet = shop.accepts_wallet !== false;
  const walletUsable = !isGuest && !walletShort && shopTakesWallet;
```

Then wrap the wallet method button so it is not rendered at all when `!shopTakesWallet`:

```typescript
      {shopTakesWallet && (
        <button
          type="button"
          className={`${styles.method} ${method === 'wallet' ? styles.methodOn : ''}`}
          onClick={() => walletUsable && onMethod('wallet')}
          aria-pressed={method === 'wallet'}
          disabled={!walletUsable}
        >
```

Close the wrapper with `)}` after that button's closing `</button>`.

- [ ] **Step 4: Default the method to gateway when wallet is unavailable**

In `PayStep.tsx`, add after the `const saving = ...` line:

```typescript
  // Never leave 'wallet' selected at a shop that cannot take it.
  useEffect(() => {
    if (!shopTakesWallet && method === 'wallet') onMethod('gateway');
  }, [shopTakesWallet, method, onMethod]);
```

Add `useEffect` to the React import at the top of the file.

- [ ] **Step 5: Typecheck and build**

```bash
cd printvendo-web
npx tsc --noEmit
npm run build
```

Expected: no type errors, `✓ Compiled successfully`

- [ ] **Step 6: Commit**

```bash
git add lib/types.ts lib/printerState.ts components/flow/PayStep.tsx
git commit -m "fix(pay): hide wallet at kiosks that collect into their own account"
```

---

### Task 12: Deploy

**Files:** none — this is the production sequence.

- [ ] **Step 1: Run the full suite one final time**

Run: `cd cloud-backend && python -m pytest -q`
Expected: `188 passed`

- [ ] **Step 2: Push**

```bash
cd cloud-backend
git push origin vps-migration-hardening
```

- [ ] **Step 3: On the VPS — pull and build**

```bash
ssh printit@<VPS_IP>
cd /opt/printit/cloud-backend
git pull
git log -1 --oneline
cd deploy
docker compose build api
```

- [ ] **Step 4: Migrate inside the new image, before the swap**

```bash
docker compose run --rm api python migrate_add_kiosk_fields.py
```

Expected: `printers: kiosk_type, accepts_wallet, onboarding_stage, onboarding_note ensured`

- [ ] **Step 5: Swap the container**

```bash
docker compose up -d api
docker compose logs -f api
```

Expected: `Application startup complete`, then Ctrl-C.

- [ ] **Step 6: Backfill**

```bash
docker compose exec -T api python backfill_accepts_wallet.py
```

Expected: a count of printers with the wallet split. **Read the numbers.** If every printer came back `accepts_wallet=False`, no kiosk resolves to platform keys — stop and investigate before telling anyone wallet works.

- [ ] **Step 7: Verify the leak is closed**

```bash
docker compose exec -T api python -c "
from app.db.session import SessionLocal
from app.models.printer import Printer
db = SessionLocal()
for p in db.query(Printer).all():
    print(f'{p.printer_id:20} type={p.kiosk_type:9} wallet={p.accepts_wallet}')
db.close()
"
```

Expected: every owner-run kiosk shows `wallet=False`. Correct any `kiosk_type` guesses by hand later in the console.

- [ ] **Step 8: Health check**

```bash
docker compose exec -T api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
```

Expected: `{"status":"ok"}`

---

## Self-Review

**Spec coverage.** This plan implements the spec's steps 1 and 2 (migrations, security fixes and backfill) plus the paper-control endpoints from the "Paper counts" section. Deliberately deferred to later plans, each noted in the spec: subscription plans tables and pricing limits, the audit log, the onboarding pipeline endpoints, owner-scoped refunds, the queue and search endpoints, and both frontend apps. Every one of those is independently shippable and none is blocked by anything here except the `printers` columns added in Task 1.

**Placeholders.** None. Every code step contains the code; every command has an expected output.

**Type consistency.** `set_printer_paper(db, printer, *, capacity, sheets_left, actor_user_id, note)` is defined in Task 7 and called with exactly those keywords in Task 8. `PaperUpdate` fields (`capacity`, `sheets_left`, `note`) match between the schema and both endpoints. `kiosk_accepts_wallet(db, printer)` is defined in Task 3 and used in Tasks 4 and 9. `_assert_owns_printer` and `_assert_services_printer` are existing helpers in `kiosk.py` and `refiller.py` respectively.

**Two things the executing engineer must not "tidy".** `accepts_wallet` defaults to `False` — a permissive default reopens the money leak. And the in-body `is_admin` check in Task 10 stays alongside the new dependency; removing it makes a future mistake silent.
