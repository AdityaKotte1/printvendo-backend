# Backend TTL Cache + Settlement N+1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Add an in-process TTL cache for the polled `/kiosk/summary` aggregation, and
batch-load the two settlement-list N+1 loops — all behaviour-preserving, pytest-covered.

**Architecture:** New `app/core/cache.py` (process-local dict, monotonic TTL, no Redis).
`/kiosk/summary` checks the cache at the top and stores before returning (no body reindent).
`/owner/settlements/pending` and `/owner/settlements/history` batch-load Users/BankDetails
into maps instead of querying per row.

**Tech Stack:** FastAPI, SQLAlchemy, pytest (existing harness). Use `./.venv/Scripts/python.exe`.

**Branch:** `perf/backend-cache-n1` off `main`. **Working dir:** `cloud-backend/`.

> Out of scope (documented follow-up): caching `/owner/summary` (param-heavy) and the
> invoice-export / admin-printer-list N+1s.

---

### Task 0: Branch
- [ ] `git checkout -b perf/backend-cache-n1`

---

### Task 1: `app/core/cache.py` (TDD)

**Files:** Create `app/core/cache.py`; Test `tests/test_cache.py`.

- [ ] **Step 1: Failing test** — create `tests/test_cache.py`:

```python
import time

from app.core import cache


def test_cache_set_get_and_expiry():
    cache.cache_clear()
    cache.cache_set("k", {"v": 1}, ttl_seconds=0.05)
    assert cache.cache_get("k") == {"v": 1}
    time.sleep(0.06)
    assert cache.cache_get("k") is None


def test_cache_clear():
    cache.cache_set("a", 1, ttl_seconds=30)
    cache.cache_clear()
    assert cache.cache_get("a") is None
```

- [ ] **Step 2: Run → fails** (`ModuleNotFoundError: app.core.cache`):

```bash
./.venv/Scripts/python.exe -m pytest tests/test_cache.py -q
```

- [ ] **Step 3: Implement `app/core/cache.py`:**

```python
"""Tiny process-local TTL cache for read-only aggregations.

Per-worker (not shared across processes). Use only for stats that tolerate brief
staleness — never for money/job-state reads. Cached values are returned by reference;
callers must treat them as read-only.
"""
import time
from typing import Any

_store: dict[str, tuple[float, Any]] = {}


def cache_get(key: str):
    rec = _store.get(key)
    if rec is None:
        return None
    expires_at, value = rec
    if time.monotonic() >= expires_at:
        _store.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any, ttl_seconds: float = 30.0) -> None:
    _store[key] = (time.monotonic() + ttl_seconds, value)


def cache_clear() -> None:
    _store.clear()
```

- [ ] **Step 4: Run → passes:**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_cache.py -q
```

- [ ] **Step 5: Commit:**

```bash
git add app/core/cache.py tests/test_cache.py
git commit -m "feat(core): add in-process TTL cache utility"
```

---

### Task 2: Cache `/kiosk/summary`

**Files:** Modify `app/routers/kiosk.py` (`kiosk_summary`, ~765-855); Test `tests/test_kiosk_summary_cache.py`.

- [ ] **Step 1: Add the cache check** — right after the docstring line
  `"""High-level summary stats scoped to this kiosk owner's printers."""`, insert:

```python
    from app.core.cache import cache_get, cache_set

    _cache_key = f"kiosk_summary:{current_user.id}"
    _cached = cache_get(_cache_key)
    if _cached is not None:
        return _cached
```

- [ ] **Step 2: Store before returning** — change the final `return {` of `kiosk_summary`
  (the dict that starts with `"total_printers": len(owned_ids),`) so it assigns and stores.
  Replace:

```python
    return {
        "total_printers": len(owned_ids),
```

with:

```python
    _result = {
        "total_printers": len(owned_ids),
```

and after that dict literal's closing `}` (the end of the return value), add:

```python
    cache_set(_cache_key, _result, ttl_seconds=30)
    return _result
```

  (i.e. the function now builds `_result = { ... }`, caches it, and returns it.)

- [ ] **Step 3: Characterization + cache-behaviour test** — create
  `tests/test_kiosk_summary_cache.py`:

```python
from decimal import Decimal

from app.core import cache
from app.routers.kiosk import kiosk_summary
from tests.conftest import make_user, make_printer, own, make_job, make_payment


def test_summary_is_cached_then_refreshes_after_clear(db):
    cache.cache_clear()
    u = make_user(db)
    p = make_printer(db)
    own(db, u, p)
    j1 = make_job(db, u, Decimal("10.00"))
    make_payment(db, j1, u, p, Decimal("10.00"), "PAID")

    first = kiosk_summary(db=db, current_user=u)
    assert first["total_revenue"] == "10.00"

    # Add more revenue; cached call must still return the stale snapshot.
    j2 = make_job(db, u, Decimal("5.00"))
    make_payment(db, j2, u, p, Decimal("5.00"), "PAID")
    cached = kiosk_summary(db=db, current_user=u)
    assert cached["total_revenue"] == "10.00"

    # After clearing, it recomputes with the new payment.
    cache.cache_clear()
    fresh = kiosk_summary(db=db, current_user=u)
    assert fresh["total_revenue"] == "15.00"
```

- [ ] **Step 4: Run:**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_kiosk_summary_cache.py -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit:**

```bash
git add app/routers/kiosk.py tests/test_kiosk_summary_cache.py
git commit -m "perf(kiosk): cache /kiosk/summary for 30s"
```

---

### Task 3: Batch-load settlement-list N+1s

**Files:** Modify `app/routers/owner.py` (`list_pending_settlements` ~933-965,
`list_history_settlements` ~968-1002); Test `tests/test_owner_settlement_lists.py`.

- [ ] **Step 1: Characterization test** — create `tests/test_owner_settlement_lists.py`:

```python
from decimal import Decimal

from app.routers.owner import list_pending_settlements, list_history_settlements
from app.models.bank_details import BankDetails
from tests.conftest import make_user, make_settlement


def test_pending_settlements_includes_user_and_bank(db):
    u = make_user(db, email="o1@test.in")
    db.add(BankDetails(user_id=u.id, account_name="Acc One", account_number="111",
                       ifsc_code="IFSC0001", status="PENDING_APPROVAL"))
    db.commit()
    make_settlement(db, u, Decimal("90.00"), status="PENDING_PAYMENT")

    out = list_pending_settlements(db=db, _=u)
    assert len(out) == 1
    assert out[0]["user_email"] == "o1@test.in"
    assert out[0]["amount"] == "90.00"
    assert out[0]["bank_details"]["account_name"] == "Acc One"


def test_history_settlements_includes_approved_bank(db):
    u = make_user(db, email="o2@test.in")
    db.add(BankDetails(user_id=u.id, account_name="Acc Two", account_number="222",
                       ifsc_code="IFSC0002", status="APPROVED"))
    db.commit()
    make_settlement(db, u, Decimal("50.00"), status="SETTLED")

    out = list_history_settlements(db=db, _=u)
    assert len(out) == 1
    assert out[0]["user_email"] == "o2@test.in"
    assert out[0]["bank_details"]["account_name"] == "Acc Two"
```

- [ ] **Step 2: Run against current code → passes** (baseline):

```bash
./.venv/Scripts/python.exe -m pytest tests/test_owner_settlement_lists.py -q
```

- [ ] **Step 3: Refactor `list_pending_settlements`** — replace the loop body
  (lines ~940-965) with a batch-loaded version. Replace:

```python
    pending = db.query(Settlement).filter(Settlement.status == "PENDING_PAYMENT").all()
    results = []
    for s in pending:
        u = db.query(User).filter(User.id == s.user_id).first()
        bd = db.query(BankDetails).filter(BankDetails.user_id == s.user_id).order_by(BankDetails.created_at.desc()).first()
        results.append({
```

with:

```python
    pending = db.query(Settlement).filter(Settlement.status == "PENDING_PAYMENT").all()
    user_ids = {s.user_id for s in pending}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    bank_by_user: dict[int, BankDetails] = {}
    if user_ids:
        for bd_row in (
            db.query(BankDetails)
            .filter(BankDetails.user_id.in_(user_ids))
            .order_by(BankDetails.created_at.desc())
            .all()
        ):
            bank_by_user.setdefault(bd_row.user_id, bd_row)  # newest first wins
    results = []
    for s in pending:
        u = users.get(s.user_id)
        bd = bank_by_user.get(s.user_id)
        results.append({
```

- [ ] **Step 4: Refactor `list_history_settlements`** — replace its loop body
  (lines ~983-985). Replace:

```python
    results = []
    for s, user in settlements_data:
        bd = db.query(BankDetails).filter(BankDetails.user_id == user.id, BankDetails.status == "APPROVED").first()
        results.append({
```

with:

```python
    user_ids = {user.id for _s, user in settlements_data}
    approved_bank: dict[int, BankDetails] = {}
    if user_ids:
        for bd_row in (
            db.query(BankDetails)
            .filter(BankDetails.user_id.in_(user_ids), BankDetails.status == "APPROVED")
            .all()
        ):
            approved_bank.setdefault(bd_row.user_id, bd_row)
    results = []
    for s, user in settlements_data:
        bd = approved_bank.get(user.id)
        results.append({
```

- [ ] **Step 5: Run again → still passes:**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_owner_settlement_lists.py -q
```

- [ ] **Step 6: Full suite:**

```bash
./.venv/Scripts/python.exe -m pytest -q
```

Expected: all pass.

- [ ] **Step 7: Commit:**

```bash
git add app/routers/owner.py tests/test_owner_settlement_lists.py
git commit -m "perf(owner): batch-load users/bank-details in settlement lists (kill N+1)"
```

---

## Self-review notes
- **Spec coverage:** §4D cache (kiosk/summary) + part of §4A N+1 (settlement lists).
  Owner/summary cache + invoice-export/admin-printer N+1 = documented follow-up.
- **No behaviour change:** cache returns identical dict within TTL; settlement lists assert
  identical output before/after batching.
- **Cache key** includes `current_user.id`; `cache_clear()` used in tests to avoid
  cross-test contamination from the module-level store.
