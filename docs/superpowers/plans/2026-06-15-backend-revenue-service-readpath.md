# Backend Revenue-Service Dedup + Printers Read-Path Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the duplicated revenue/commission/unsettled math into one tested
service used by both `kiosk.get_unsettled_revenue` and `owner.generate_scheduled_settlements`,
and remove the write-on-read + per-printer N+1 from `GET /printers/` — all with
**byte-identical behaviour**, introducing pytest as the project's first test harness.

**Architecture:** New pure module `app/services/revenue_service.py` (imports only models +
SQLAlchemy — no routers, no circular imports). It exposes `owned_printer_ids()` and
`owner_earnings(db, user_id, printer_ids, is_subscribed)` returning the shared Decimal
components. Callers keep their own final treatment (kiosk clamps `unsettled` to ≥0; the
settlement generator keeps its ≥₹10 + ratio-scaling logic). `GET /printers/` stops
mutating/committing status on the read path and batch-loads owner users.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, pytest (new), SQLite in-memory for tests.

**This is subsystem plan 2 of N** for the Phase 1 spec. Independently shippable.
Deferred to a later backend plan: TTL cache on summary/revenue endpoints, and the
remaining N+1 batch-loads in owner invoice-export / settlement-list / admin-printer-list.

**Working directory for all commands:** `cloud-backend/` (its own git repo). Use the
project venv python: `./.venv/Scripts/python.exe` (Windows Git Bash).

**Branching:** create `perf/revenue-service` off `main` before Task 1.

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

Run:

```bash
git checkout main && git checkout -b perf/revenue-service
```

Expected: `Switched to a new branch 'perf/revenue-service'`.

---

### Task 1: Test harness (pytest + conftest + seed helpers)

**Files:**
- Create: `cloud-backend/requirements-dev.txt`
- Create: `cloud-backend/tests/__init__.py` (empty)
- Create: `cloud-backend/tests/conftest.py`
- Create: `cloud-backend/tests/test_harness_smoke.py`

- [ ] **Step 1: Ensure app dependencies + pytest are installed in the venv**

The venv is known to be missing some deps (e.g. `slowapi`). Install everything:

```bash
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m pip install pytest
```

Expected: completes without error (already-satisfied lines are fine).

- [ ] **Step 2: Create `requirements-dev.txt`**

```
pytest>=8.0
```

- [ ] **Step 3: Create `tests/__init__.py`** (empty file)

- [ ] **Step 4: Create `tests/conftest.py`**

```python
"""Test harness: in-memory SQLite session + seed helpers.

Imports only models (not routers), so it never pulls heavy optional deps.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base

# Import models so their tables register on Base.metadata.
import app.models.user  # noqa: F401
import app.models.printer  # noqa: F401
import app.models.printer_owner  # noqa: F401
import app.models.job  # noqa: F401
import app.models.payment  # noqa: F401
import app.models.settlement  # noqa: F401
import app.models.subscription  # noqa: F401  (has_active_subscription queries this table)

from app.models.user import User
from app.models.printer import Printer
from app.models.printer_owner import PrinterOwner
from app.models.job import Job
from app.models.payment import Payment
from app.models.settlement import Settlement


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def make_user(db, email="owner@test.in", **kw):
    u = User(email=email, hashed_password="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_printer(db, printer_id="P1", name="Printer 1", **kw):
    p = Printer(printer_id=printer_id, name=name, secret_token="t",
                is_active=True, is_approved=True, **kw)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def own(db, user, printer):
    po = PrinterOwner(user_id=user.id, printer_id=printer.id)
    db.add(po)
    db.commit()
    return po


def make_job(db, user, price):
    j = Job(user_id=user.id, original_filename="f.pdf", original_file_path="/x",
            price_cents=price, status="READY_TO_PRINT")
    db.add(j)
    db.commit()
    db.refresh(j)
    return j


def make_payment(db, job, user, printer, amount, status):
    p = Payment(job_id=job.id, user_id=user.id, printer_id=printer.id,
                amount=amount, status=status)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def make_settlement(db, user, amount, status="PENDING_PAYMENT"):
    s = Settlement(user_id=user.id, amount=amount, status=status,
                   period_start=datetime(2026, 1, 1), period_end=datetime(2026, 1, 31))
    db.add(s)
    db.commit()
    db.refresh(s)
    return s
```

- [ ] **Step 5: Create `tests/test_harness_smoke.py`**

```python
from tests.conftest import make_user, make_printer, own, make_job, make_payment


def test_seed_helpers_build_a_graph(db):
    u = make_user(db)
    p = make_printer(db)
    own(db, u, p)
    j = make_job(db, u, 10)
    pay = make_payment(db, j, u, p, 12, "PAID")
    assert pay.id is not None
    assert pay.status == "PAID"
```

- [ ] **Step 6: Run the smoke test**

Run (from `cloud-backend/`):

```bash
./.venv/Scripts/python.exe -m pytest tests/test_harness_smoke.py -v
```

Expected: `1 passed`.

- [ ] **Step 7: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py tests/test_harness_smoke.py
git commit -m "test: introduce pytest harness with sqlite session + seed helpers"
```

---

### Task 2: `revenue_service` (TDD)

**Files:**
- Create: `cloud-backend/app/services/revenue_service.py`
- Test: `cloud-backend/tests/test_revenue_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_revenue_service.py`:

```python
from decimal import Decimal

from app.services import revenue_service as rs
from tests.conftest import make_user, make_printer, own, make_job, make_payment, make_settlement


def test_owned_printer_ids(db):
    u = make_user(db)
    p = make_printer(db)
    own(db, u, p)
    assert rs.owned_printer_ids(db, u.id) == [p.id]


def test_owner_earnings_non_subscriber(db):
    u = make_user(db)
    p = make_printer(db)
    own(db, u, p)
    # PAID 10 (price_cents) + REFUNDED 5 (price_cents). amount differs to prove price_cents wins.
    jp = make_job(db, u, Decimal("10.00"))
    make_payment(db, jp, u, p, Decimal("12.00"), "PAID")
    jr = make_job(db, u, Decimal("5.00"))
    make_payment(db, jr, u, p, Decimal("7.00"), "REFUNDED")

    e = rs.owner_earnings(db, u.id, [p.id], is_subscribed=False)
    assert e["gross"] == Decimal("15.00")          # 10 + 5
    assert e["refunded"] == Decimal("5.00")
    assert e["net"] == Decimal("10.00")            # 15 - 5
    assert e["commission_rate"] == Decimal("0.10")
    assert e["commission"] == Decimal("1.00")      # 10 * 0.10
    assert e["owner_share"] == Decimal("9.00")
    assert e["settled_and_pending"] == Decimal("0")


def test_owner_earnings_subscriber_zero_commission(db):
    u = make_user(db)
    p = make_printer(db)
    own(db, u, p)
    j = make_job(db, u, Decimal("100.00"))
    make_payment(db, j, u, p, Decimal("100.00"), "PAID")
    make_settlement(db, u, Decimal("40.00"))

    e = rs.owner_earnings(db, u.id, [p.id], is_subscribed=True)
    assert e["commission_rate"] == Decimal("0.00")
    assert e["commission"] == Decimal("0.00")
    assert e["owner_share"] == Decimal("100.00")
    assert e["settled_and_pending"] == Decimal("40.00")
```

- [ ] **Step 2: Run to verify it fails**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_revenue_service.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.revenue_service'`.

- [ ] **Step 3: Implement the service**

Create `app/services/revenue_service.py`:

```python
"""Single source of truth for kiosk-owner revenue / commission math.

Imports only models + SQLAlchemy (no routers) to avoid circular imports.
Callers pass `is_subscribed` (computed via subscription.has_active_subscription)
and apply their own final treatment of `unsettled`.
"""
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.payment import Payment
from app.models.printer_owner import PrinterOwner
from app.models.settlement import Settlement

# Statuses that count toward gross collected revenue.
GROSS_STATUSES = ["PAID", "CAPTURED", "REFUNDED"]


def owned_printer_ids(db: Session, user_id: int) -> list[int]:
    return [
        po.printer_id
        for po in db.query(PrinterOwner).filter(PrinterOwner.user_id == user_id).all()
    ]


def _base_amt():
    # Prefer Job.price_cents (no gateway fee); fall back to Payment.amount.
    return func.coalesce(Job.price_cents, Payment.amount)


def owner_earnings(
    db: Session,
    user_id: int,
    printer_ids: list[int],
    is_subscribed: bool,
) -> dict:
    """Return the shared revenue components as Decimals.

    Mirrors the historical math in kiosk.get_unsettled_revenue and
    owner.generate_scheduled_settlements exactly.
    """
    commission_rate = Decimal("0.00") if is_subscribed else Decimal("0.10")

    if not printer_ids:
        zero = Decimal("0")
        return {
            "gross": zero, "refunded": zero, "net": zero,
            "commission_rate": commission_rate, "commission": zero,
            "owner_share": zero, "settled_and_pending": zero,
        }

    total_gross = (
        db.query(func.coalesce(func.sum(_base_amt()), 0))
        .select_from(Payment)
        .join(Job, Job.id == Payment.job_id)
        .filter(Payment.status.in_(GROSS_STATUSES), Payment.printer_id.in_(printer_ids))
        .scalar()
        or Decimal("0")
    )
    total_refunded = (
        db.query(func.coalesce(func.sum(_base_amt()), 0))
        .select_from(Payment)
        .join(Job, Job.id == Payment.job_id)
        .filter(Payment.status == "REFUNDED", Payment.printer_id.in_(printer_ids))
        .scalar()
        or Decimal("0")
    )
    net_collected = max(Decimal("0"), total_gross - total_refunded)
    commission = net_collected * commission_rate
    owner_share = net_collected - commission
    settled_and_pending = (
        db.query(func.coalesce(func.sum(Settlement.amount), 0))
        .filter(Settlement.user_id == user_id)
        .scalar()
        or Decimal("0")
    )
    return {
        "gross": total_gross,
        "refunded": total_refunded,
        "net": net_collected,
        "commission_rate": commission_rate,
        "commission": commission,
        "owner_share": owner_share,
        "settled_and_pending": settled_and_pending,
    }
```

- [ ] **Step 4: Run to verify it passes**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_revenue_service.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add app/services/revenue_service.py tests/test_revenue_service.py
git commit -m "feat(revenue): add single-source revenue_service with unit tests"
```

---

### Task 3: Refactor `kiosk.get_unsettled_revenue` to use the service

**Files:**
- Modify: `cloud-backend/app/routers/kiosk.py:291-348`
- Test: `cloud-backend/tests/test_kiosk_unsettled.py`

- [ ] **Step 1: Write a characterization test (passes on current code)**

Create `tests/test_kiosk_unsettled.py`:

```python
from decimal import Decimal

from app.routers.kiosk import get_unsettled_revenue
from tests.conftest import make_user, make_printer, own, make_job, make_payment


def test_unsettled_revenue_matches_expected(db):
    u = make_user(db)
    p = make_printer(db)
    own(db, u, p)
    jp = make_job(db, u, Decimal("10.00"))
    make_payment(db, jp, u, p, Decimal("12.00"), "PAID")
    jr = make_job(db, u, Decimal("5.00"))
    make_payment(db, jr, u, p, Decimal("7.00"), "REFUNDED")

    # Call the endpoint function directly with the seeded session (no subscription -> 10%).
    out = get_unsettled_revenue(current_user=u, db=db)

    assert out["unsettled_revenue"] == 9.0
    assert out["gross_revenue"] == 15.0
    assert out["refunds_deducted"] == 5.0
    assert out["platform_commission"] == 1.0
    assert out["commission_rate"] == 0.10
    assert out["is_subscriber"] is False
    assert out["net_earnings"] == 9.0
    assert out["total_settled_and_pending"] == 0.0
```

- [ ] **Step 2: Run it against current code to confirm the baseline**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_kiosk_unsettled.py -v
```

Expected: `1 passed` (this pins the CURRENT behaviour before refactoring).

> If this errors on import (a router transitively needs an uninstalled dep), STOP and
> install the missing package into the venv, then re-run. Do not change the test.

- [ ] **Step 3: Refactor the endpoint body**

In `app/routers/kiosk.py`, replace the body of `get_unsettled_revenue` (currently
lines ~291-348) with the version below. Keep the decorator, signature, and docstring.

```python
@router.get("/settlements/unsettled")
def get_unsettled_revenue(
    current_user: User = Depends(get_kiosk_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Calculate the total revenue pending settlement."""
    from app.services.revenue_service import owner_earnings

    printer_ids = _get_owned_printer_ids(db, current_user)
    if not printer_ids:
        return {"unsettled_revenue": 0.0}

    is_subscribed = has_active_subscription(db, current_user.id)
    e = owner_earnings(db, current_user.id, printer_ids, is_subscribed)
    unsettled = max(Decimal("0"), e["owner_share"] - e["settled_and_pending"])

    return {
        "unsettled_revenue": float(unsettled),
        "gross_revenue": float(e["gross"]),
        "refunds_deducted": float(e["refunded"]),
        "platform_commission": float(e["commission"]),
        "commission_rate": float(e["commission_rate"]),
        "is_subscriber": is_subscribed,
        "net_earnings": float(e["owner_share"]),
        "total_settled_and_pending": float(e["settled_and_pending"]),
    }
```

- [ ] **Step 4: Run the characterization test again**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_kiosk_unsettled.py tests/test_revenue_service.py -v
```

Expected: all pass (behaviour unchanged after refactor).

- [ ] **Step 5: Commit**

```bash
git add app/routers/kiosk.py tests/test_kiosk_unsettled.py
git commit -m "refactor(kiosk): use revenue_service for unsettled revenue (no behaviour change)"
```

---

### Task 4: Refactor `owner.generate_scheduled_settlements` to use the service

**Files:**
- Modify: `cloud-backend/app/routers/owner.py:1005-1115`
- Test: `cloud-backend/tests/test_owner_generate_settlements.py`

- [ ] **Step 1: Write a characterization test (passes on current code)**

Create `tests/test_owner_generate_settlements.py`:

```python
from decimal import Decimal

from app.routers.owner import generate_scheduled_settlements
from app.models.settlement import Settlement
from app.models.bank_details import BankDetails
from tests.conftest import make_user, make_printer, own, make_job, make_payment


def test_generate_creates_settlement_for_unsettled_owner(db):
    u = make_user(db)
    p = make_printer(db)
    own(db, u, p)
    # APPROVED bank details are required for the generator to consider the user.
    db.add(BankDetails(user_id=u.id, account_name="A", account_number="1",
                       ifsc_code="IFSC0001", status="APPROVED"))
    db.commit()
    j = make_job(db, u, Decimal("100.00"))
    make_payment(db, j, u, p, Decimal("100.00"), "PAID")

    out = generate_scheduled_settlements(db=db, _=u)
    assert out["generated_count"] == 1

    s = db.query(Settlement).filter(Settlement.user_id == u.id).one()
    # net=100, commission 10% = 10, owner_share=90, settled=0 -> unsettled 90 (>= 10)
    assert s.amount == Decimal("90.00")
    assert s.status == "PENDING_PAYMENT"
```

> Note: `BankDetails` requires `account_name`, `account_number`, `ifsc_code` (see model).
> If the model rejects a field here, STOP and read `app/models/bank_details.py`, then
> correct the seed — do not weaken the assertions.

- [ ] **Step 2: Run against current code to confirm baseline**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_owner_generate_settlements.py -v
```

Expected: `1 passed`.

- [ ] **Step 3: Refactor the per-user computation**

In `app/routers/owner.py`, inside the `for bd, user in approved_details:` loop of
`generate_scheduled_settlements`, replace the block that currently spans the inline
`printer_ids = [...]` through `owner_gross_share = net_collected - commission` and the
`total_settled_and_pending = (...)` query (lines ~1021-1071) with:

```python
        from app.services.revenue_service import owned_printer_ids, owner_earnings

        printer_ids = owned_printer_ids(db, user.id)
        if not printer_ids:
            continue

        is_subscribed = has_active_subscription(db, user.id)
        e = owner_earnings(db, user.id, printer_ids, is_subscribed)
        total_gross = e["gross"]
        total_refunded = e["refunded"]
        commission = e["commission"]
        owner_gross_share = e["owner_share"]
        total_settled_and_pending = e["settled_and_pending"]
```

Leave everything after it unchanged: `unsettled_net = owner_gross_share -
total_settled_and_pending`, the `>= Decimal("10.00")` check, the ratio scaling
(`chunk_gross`, `chunk_refund`, `chunk_commission`), and the `Settlement(...)` creation.
Remove the now-unused inline `def base_amt():` and the `from app.models...` imports that
are no longer referenced in the loop (keep any still used elsewhere in the function).

- [ ] **Step 4: Run the characterization test again**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_owner_generate_settlements.py tests/test_revenue_service.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/routers/owner.py tests/test_owner_generate_settlements.py
git commit -m "refactor(owner): use revenue_service in settlement generation (no behaviour change)"
```

---

### Task 5: Remove write-on-read + per-printer N+1 in `GET /printers/`

**Files:**
- Modify: `cloud-backend/app/routers/printers.py:141-216`
- Test: `cloud-backend/tests/test_printers_list.py`

- [ ] **Step 1: Write a characterization test**

Create `tests/test_printers_list.py`:

```python
from datetime import datetime, timedelta

from app.routers.printers import list_online_printers
from app.models.printer import Printer
from tests.conftest import make_user, make_printer


def test_stale_printer_excluded_and_status_not_mutated(db):
    user = make_user(db)
    fresh = make_printer(db, printer_id="FRESH", name="Fresh",
                         status="ONLINE", last_heartbeat_at=datetime.utcnow())
    stale = make_printer(db, printer_id="STALE", name="Stale",
                         status="ONLINE",
                         last_heartbeat_at=datetime.utcnow() - timedelta(hours=1))

    out = list_online_printers(db=db, current_user=user)

    public_ids = {p["printer_id"] for p in out}
    assert "FRESH" in public_ids       # fresh printer is returned
    assert "STALE" not in public_ids   # stale printer is excluded

    # Read path must NOT have rewritten the stale printer's status in the DB.
    refreshed = db.query(Printer).filter(Printer.printer_id == "STALE").one()
    assert refreshed.status == "ONLINE"
```

- [ ] **Step 2: Run against current code**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_printers_list.py -v
```

Expected: **FAIL** on the last assertion — current code mutates `stale.status` to
`"OFFLINE"` and commits it (this is the write-on-read bug the task fixes).

- [ ] **Step 3: Fix the read path**

In `app/routers/printers.py`, in `list_online_printers`:

(a) Replace the stale-printer block (currently lines ~177-179):

```python
        if printer.last_heartbeat_at < cutoff:
            printer.status = "OFFLINE"
            continue
```

with (no DB mutation):

```python
        if printer.last_heartbeat_at < cutoff:
            continue
```

(b) Replace the per-printer owner lookup + subscription gate (currently lines ~169-190):

```python
    # Cache subscription status per owner
    sub_cache: dict[int, bool] = {}

    visible: list[dict] = []
    for printer in printers:
        if printer.last_heartbeat_at is None:
            continue

        if printer.last_heartbeat_at < cutoff:
            continue

        # Only gate on subscription for owners who have subscription_enabled.
        # Non-subscriber owners (subscription_enabled=False) always show their printers.
        owner_uid = owner_map.get(printer.id)
        if owner_uid:
            owner_user = db.query(User).filter(User.id == owner_uid).first()
            if owner_user and owner_user.subscription_enabled:
                if owner_uid not in sub_cache:
                    sub_cache[owner_uid] = has_active_subscription(db, owner_uid)
                if not sub_cache[owner_uid]:
                    continue  # Owner opted into subscription but it expired — hide printer
```

with a batch-loaded version (one query for all owner users; subscription check still
memoised):

```python
    # Batch-load owner users (kills the per-printer User query).
    owner_user_ids = set(owner_map.values())
    owner_users: dict[int, User] = {}
    if owner_user_ids:
        for u in db.query(User).filter(User.id.in_(owner_user_ids)).all():
            owner_users[u.id] = u

    # Memoise subscription status per owner.
    sub_cache: dict[int, bool] = {}

    visible: list[dict] = []
    for printer in printers:
        if printer.last_heartbeat_at is None:
            continue

        if printer.last_heartbeat_at < cutoff:
            continue

        # Only gate on subscription for owners who have subscription_enabled.
        # Non-subscriber owners (subscription_enabled=False) always show their printers.
        owner_uid = owner_map.get(printer.id)
        if owner_uid:
            owner_user = owner_users.get(owner_uid)
            if owner_user and owner_user.subscription_enabled:
                if owner_uid not in sub_cache:
                    sub_cache[owner_uid] = has_active_subscription(db, owner_uid)
                if not sub_cache[owner_uid]:
                    continue  # Owner opted into subscription but it expired — hide printer
```

(c) Remove the now-pointless commit at the end (currently lines ~213-214):

```python
    if visible:
        db.commit()

    return visible
```

becomes:

```python
    return visible
```

- [ ] **Step 4: Run the characterization test again**

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/test_printers_list.py -v
```

Expected: `1 passed` (stale excluded, status no longer mutated).

- [ ] **Step 5: Run the full suite**

Run:

```bash
./.venv/Scripts/python.exe -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/routers/printers.py tests/test_printers_list.py
git commit -m "perf(printers): drop write-on-read + batch-load owner users in GET /printers/"
```

---

## Self-review notes

- **Spec coverage:** implements §4C (revenue_service dedup for the two genuinely-duplicated
  call sites) and §4B (write-on-read removal) + part of §4A's N+1 list (the `GET /printers/`
  per-printer User query). `get_user_revenue_range` is intentionally untouched (different
  math — not a duplicate). TTL cache (§4D) and the remaining owner N+1s are a later plan.
- **No placeholders:** every step has exact code/commands and expected output.
- **Type/name consistency:** `owner_earnings` returns keys `gross/refunded/net/
  commission_rate/commission/owner_share/settled_and_pending`; all call sites and tests
  use exactly those keys.
- **Behaviour identical:** characterization tests for both refactored endpoints assert the
  same numbers before and after; the printers test asserts the only intended change
  (no status mutation on read), with response contents unchanged.
- **Env risk:** Task 1 installs full deps so router-importing tests work; if a router still
  fails to import, the service unit tests (Task 2) remain a valid safety net and the
  blocker is surfaced rather than worked around.
