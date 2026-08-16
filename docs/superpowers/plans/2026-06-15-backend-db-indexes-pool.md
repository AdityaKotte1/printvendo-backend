# Backend DB Indexes + Connection Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add indexes on the hot query columns and size the DB connection pool, so the
most-hit backend endpoints stop doing full table scans and stop exhausting connections —
with zero behaviour change.

**Architecture:** Follow the project's existing convention of standalone, manually-run
migration scripts (`migrate_*.py`), not Alembic. One idempotent script issues
`CREATE INDEX IF NOT EXISTS` (valid on both Postgres prod and SQLite local) and then
verifies via SQLAlchemy's dialect-agnostic inspector. Connection-pool kwargs are applied
only for non-SQLite URLs (SQLite's default pool rejects `pool_size`).

**Tech Stack:** FastAPI, SQLAlchemy, Postgres (prod) / SQLite `printit.db` (local).

**This is subsystem plan 1 of 3** for the Phase 1 spec
(`docs/superpowers/specs/2026-06-15-printit-perf-redundancy-phase1-design.md`).
It is independently shippable. Subsequent plans: (2) revenue-service + N+1 + cache,
(3) frontend (PWA + dashboards).

**Note on tests:** the backend has no test suite yet and pytest is not installed.
Indexes and pool sizing are infrastructure config, not logic, so this plan verifies with
a runnable script + SQLAlchemy inspector (not pytest). Pytest is introduced in plan 2,
where the revenue-service refactor needs characterization tests.

**Working directory for all commands:** `cloud-backend/` (its own git repo).

---

### Task 1: Size the DB connection pool

**Files:**
- Modify: `cloud-backend/app/db/session.py`

- [ ] **Step 1: Replace the engine construction**

Current `app/db/session.py` is:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import settings


engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
```

Replace the whole file with:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import settings


# Pool sizing applies only to server-backed DBs (Postgres). SQLite uses a pool
# implementation that does not accept pool_size/max_overflow, so we keep its defaults.
_engine_kwargs = {"pool_pre_ping": True}
if not settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs.update(pool_size=20, max_overflow=10, pool_recycle=3600)

engine = create_engine(settings.DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
```

- [ ] **Step 2: Verify the module imports and the engine builds**

Run (from `cloud-backend/`, venv active):

```bash
python -c "from app.db.session import engine; print('dialect=', engine.dialect.name); print('pool=', type(engine.pool).__name__)"
```

Expected: prints without error, e.g. `dialect= sqlite` and a pool type name. (On a
Postgres `DATABASE_URL` it prints `dialect= postgresql` and `pool= QueuePool`.)

- [ ] **Step 3: Commit**

```bash
git add app/db/session.py
git commit -m "perf(db): size connection pool for Postgres (pool_size=20, overflow=10)"
```

---

### Task 2: Create the idempotent index migration script

**Files:**
- Create: `cloud-backend/migrate_add_indexes.py`

- [ ] **Step 1: Write the migration script**

Create `cloud-backend/migrate_add_indexes.py` with exactly this content:

```python
"""Idempotent index migration for hot query columns.

Run manually (matches the project's migrate_*.py convention):

    python migrate_add_indexes.py

Safe to run repeatedly: every statement uses CREATE INDEX IF NOT EXISTS, which is
valid on both Postgres (prod) and SQLite (local). No data is modified.
"""
from sqlalchemy import inspect, text

from app.db.session import engine

# (index_name, table, "comma, separated, columns")
INDEXES = [
    # payments — filtered constantly by status / printer / user / job / order id / date
    ("idx_payments_status", "payments", "status"),
    ("idx_payments_printer_id", "payments", "printer_id"),
    ("idx_payments_user_id", "payments", "user_id"),
    ("idx_payments_job_id", "payments", "job_id"),
    ("idx_payments_razorpay_order_id", "payments", "razorpay_order_id"),
    ("idx_payments_status_created", "payments", "status, created_at"),
    # printer_jobs — queue/printer/status scans
    ("idx_printer_jobs_printer_id", "printer_jobs", "printer_id"),
    ("idx_printer_jobs_job_id", "printer_jobs", "job_id"),
    ("idx_printer_jobs_status", "printer_jobs", "status"),
    ("idx_printer_jobs_status_created", "printer_jobs", "status, created_at"),
    # jobs — per-user listing
    ("idx_jobs_user_id", "jobs", "user_id"),
    # wallet_ledger — balance/ledger reads
    ("idx_wallet_ledger_user_status", "wallet_ledger", "user_id, status"),
]


def create_indexes() -> None:
    with engine.begin() as conn:
        for name, table, cols in INDEXES:
            stmt = f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})'
            print(f"  applying {name} on {table}({cols})")
            conn.execute(text(stmt))


def verify() -> bool:
    inspector = inspect(engine)
    existing = set()
    for table in {t for _, t, _ in INDEXES}:
        for idx in inspector.get_indexes(table):
            if idx.get("name"):
                existing.add(idx["name"])
    ok = True
    for name, table, _ in INDEXES:
        present = name in existing
        ok = ok and present
        print(f"  [{'OK' if present else 'MISSING'}] {name} ({table})")
    return ok


if __name__ == "__main__":
    print("Creating indexes...")
    create_indexes()
    print("Verifying indexes...")
    if verify():
        print("All indexes present.")
    else:
        raise SystemExit("One or more indexes are missing")
```

- [ ] **Step 2: Run the migration against the local DB**

Run (from `cloud-backend/`, venv active):

```bash
python migrate_add_indexes.py
```

Expected: prints `applying …` for each index, then a verify block where **every** line
reads `[OK] idx_… (table)`, ending with `All indexes present.` and exit code 0.

> If the local `printit.db` does not exist yet, start the app once
> (`uvicorn app.main:app --port 8080`) so `Base.metadata.create_all` builds the tables,
> stop it, then re-run the migration.

- [ ] **Step 3: Run it a second time to prove idempotency**

Run:

```bash
python migrate_add_indexes.py
```

Expected: identical output — no errors (the `IF NOT EXISTS` clauses make re-runs no-ops),
still ending `All indexes present.`

- [ ] **Step 4: Commit**

```bash
git add migrate_add_indexes.py
git commit -m "perf(db): add idempotent migration for hot-path indexes"
```

---

### Task 3: Document the production rollout step

**Files:**
- Modify: `cloud-backend/CLAUDE.md` (append one line to the migrations note)

- [ ] **Step 1: Add the rollout note**

In `cloud-backend/CLAUDE.md`, find the gotcha line that begins
`- Migrations are plain scripts, not Alembic revisions` and add a new bullet
immediately after it:

```markdown
- Performance indexes live in `migrate_add_indexes.py` (idempotent). Run after deploy:
  `fly ssh console -C "python migrate_add_indexes.py"`.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note index migration rollout on Fly.io"
```

---

## Production rollout (manual, after merge/deploy)

Indexes do not auto-apply (the migration is manual by project convention). After
deploying the backend, run once against prod:

```bash
fly ssh console -C "python migrate_add_indexes.py"
```

On a large `payments`/`printer_jobs` table the plain `CREATE INDEX` briefly locks the
table; this dataset is small, so run at low traffic. (If the tables ever grow large,
switch the Postgres statements to `CREATE INDEX CONCURRENTLY`, which cannot run inside a
transaction — out of scope here.)

---

## Self-review notes

- **Spec coverage:** implements spec §4A "Indexes" (all 12 listed indexes across
  `payments`, `printer_jobs`, `jobs`, `wallet_ledger`) and "Connection pool". N+1 fixes,
  write-on-read, revenue service, and caching from §4 are in later plans.
- **Names verified against source:** `payments(status, printer_id, user_id, job_id,
  razorpay_order_id, created_at)`, `printer_jobs(printer_id, job_id, status,
  created_at)`, `jobs(user_id)`, `wallet_ledger(user_id, status)` — confirmed in the
  model files.
- **No placeholders:** every step has exact code/commands and expected output.
- **SQLite safety:** pool kwargs gated behind a non-sqlite URL check; index DDL uses
  `IF NOT EXISTS` (supported by both dialects); verification uses the dialect-agnostic
  SQLAlchemy inspector.
