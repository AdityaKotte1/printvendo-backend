# Backend N+1: Invoice Export + Admin Printer Lists — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Kill the per-row N+1 queries in `export_invoices_csv`, `list_unapproved_printers`,
and `list_all_printers_admin` by batch-loading; behaviour-preserving, pytest-covered.

**Branch:** `perf/backend-n1-more` off `main`. **Dir:** `cloud-backend/`. Python: `./.venv/Scripts/python.exe`.

---

### Task 0: Branch
- [ ] `git checkout -b perf/backend-n1-more`

---

### Task 1: Characterization tests (pass on current code)

**Files:** Create `tests/test_owner_exports_printers.py`.

```python
from decimal import Decimal

from app.routers.owner import export_invoices_csv, list_unapproved_printers, list_all_printers_admin
from tests.conftest import make_user, make_printer, own, make_job, make_payment


def test_invoice_csv_has_user_and_printer(db):
    u = make_user(db, email="csv@test.in", full_name="Cee Esvee")
    p = make_printer(db, printer_id="PCSV", name="CSV Printer")
    j = make_job(db, u, Decimal("12.00"))
    make_payment(db, j, u, p, Decimal("12.00"), "PAID")

    resp = export_invoices_csv(db=db, _=u)
    body = resp.body.decode() if isinstance(resp.body, (bytes, bytearray)) else str(resp.body)
    assert "csv@test.in" in body
    assert "CSV Printer" in body
    assert "PCSV" in body


def test_unapproved_printers_lists_owner(db):
    u = make_user(db, email="own@test.in", full_name="Owner One")
    p = make_printer(db, printer_id="PUNAPP", name="Unapproved", is_approved=False)
    own(db, u, p)

    out = list_unapproved_printers(db=db, _=u)
    row = next(r for r in out if r["printer_id"] == "PUNAPP")
    assert row["owner_email"] == "own@test.in"
    assert row["owner_name"] == "Owner One"


def test_all_printers_includes_owner_and_subscription_flag(db):
    u = make_user(db, email="own2@test.in")
    p = make_printer(db, printer_id="PALL", name="All Printer")
    own(db, u, p)

    out = list_all_printers_admin(db=db, _=u)
    row = next(r for r in out if r["printer_id"] == "PALL")
    assert row["owner_email"] == "own2@test.in"
    assert row["has_subscription"] is False  # no subscription seeded
```

- [ ] **Step 1: Run → passes on current code:**
```bash
./.venv/Scripts/python.exe -m pytest tests/test_owner_exports_printers.py -q
```

---

### Task 2: Batch-load `export_invoices_csv`

- [ ] **Step 1:** Replace the per-row lookups (the `for p in payments:` block, specifically
  the lines that fetch `user`/`job`/`printer`). Replace:

```python
    for p in payments:
        created = p.created_at or datetime.utcnow()
        invoice_number = f"INV-{created:%Y%m%d}-{p.id:06d}"

        user = db.query(User).filter(User.id == p.user_id).first()
        job = db.query(Job).filter(Job.id == p.job_id).first()
        printer: Printer | None = None
        if p.printer_id is not None:
            printer = db.query(Printer).filter(Printer.id == p.printer_id).first()
```

with:

```python
    user_ids = {p.user_id for p in payments if p.user_id is not None}
    job_ids = {p.job_id for p in payments if p.job_id is not None}
    printer_ids = {p.printer_id for p in payments if p.printer_id is not None}
    users_map = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    jobs_map = {j.id: j for j in db.query(Job).filter(Job.id.in_(job_ids)).all()} if job_ids else {}
    printers_map = {pr.id: pr for pr in db.query(Printer).filter(Printer.id.in_(printer_ids)).all()} if printer_ids else {}

    for p in payments:
        created = p.created_at or datetime.utcnow()
        invoice_number = f"INV-{created:%Y%m%d}-{p.id:06d}"

        user = users_map.get(p.user_id)
        job = jobs_map.get(p.job_id)
        printer: Printer | None = printers_map.get(p.printer_id) if p.printer_id is not None else None
```

- [ ] **Step 2: Run** `pytest tests/test_owner_exports_printers.py -q` → still passes.
- [ ] **Step 3: Commit:** `git add app/routers/owner.py tests/test_owner_exports_printers.py && git commit -m "perf(owner): batch-load invoice CSV export (kill N+1)"`

---

### Task 3: Batch-load both admin printer lists

- [ ] **Step 1:** Replace `list_unapproved_printers`' loop preamble. Replace:

```python
    printers = db.query(Printer).filter(Printer.is_approved == False).all()  # noqa: E712
    results = []
    for p in printers:
        # Find assigned owner if any
        po = db.query(PrinterOwner).filter(PrinterOwner.printer_id == p.id).first()
        owner_user = None
        if po:
            owner_user = db.query(User).filter(User.id == po.user_id).first()
        results.append({
```

with:

```python
    printers = db.query(Printer).filter(Printer.is_approved == False).all()  # noqa: E712
    _printer_ids = [p.id for p in printers]
    _owner_by_printer = {}
    if _printer_ids:
        for po in db.query(PrinterOwner).filter(PrinterOwner.printer_id.in_(_printer_ids)).all():
            _owner_by_printer.setdefault(po.printer_id, po.user_id)
    _users_map = {u.id: u for u in db.query(User).filter(User.id.in_(set(_owner_by_printer.values()))).all()} if _owner_by_printer else {}
    results = []
    for p in printers:
        owner_user = _users_map.get(_owner_by_printer.get(p.id))
        results.append({
```

- [ ] **Step 2:** Replace `list_all_printers_admin`' loop preamble. Replace:

```python
    printers = db.query(Printer).order_by(Printer.created_at.desc()).all()
    results = []
    for p in printers:
        po = db.query(PrinterOwner).filter(PrinterOwner.printer_id == p.id).first()
        owner_user = None
        if po:
            owner_user = db.query(User).filter(User.id == po.user_id).first()
        results.append({
```

with:

```python
    printers = db.query(Printer).order_by(Printer.created_at.desc()).all()
    _printer_ids = [p.id for p in printers]
    _owner_by_printer = {}
    if _printer_ids:
        for po in db.query(PrinterOwner).filter(PrinterOwner.printer_id.in_(_printer_ids)).all():
            _owner_by_printer.setdefault(po.printer_id, po.user_id)
    _users_map = {u.id: u for u in db.query(User).filter(User.id.in_(set(_owner_by_printer.values()))).all()} if _owner_by_printer else {}
    _sub_cache: dict[int, bool] = {}
    def _is_sub(uid):
        if uid not in _sub_cache:
            _sub_cache[uid] = has_active_subscription(db, uid)
        return _sub_cache[uid]
    results = []
    for p in printers:
        owner_uid = _owner_by_printer.get(p.id)
        owner_user = _users_map.get(owner_uid)
        results.append({
```

- [ ] **Step 3:** In `list_all_printers_admin`'s appended dict, change the subscription line:

```python
            "has_subscription": has_active_subscription(db, po.user_id) if po else False,
```

to:

```python
            "has_subscription": _is_sub(owner_uid) if owner_uid is not None else False,
```

- [ ] **Step 4: Run full suite:** `./.venv/Scripts/python.exe -m pytest -q` → all pass.
- [ ] **Step 5: Commit:** `git add app/routers/owner.py && git commit -m "perf(owner): batch-load owner/subscription in admin printer lists (kill N+1)"`

---

## Self-review notes
- **Spec coverage:** completes §4A remaining N+1s (invoice export, admin printer lists).
- **No behaviour change:** `_owner_by_printer.setdefault` keeps the first PrinterOwner per
  printer (matches `.first()` without explicit ordering); `has_active_subscription` still
  called (memoized per owner — identical result, fewer calls).
- **Verification:** characterization tests assert identical output before/after; full suite.
