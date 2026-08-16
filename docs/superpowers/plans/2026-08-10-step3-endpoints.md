# Step 3 — Endpoints for both dashboards

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build every backend endpoint the owner app and the admin console need,
so `printvendo-owner` and `printvendo-admin` can be built without further
backend work.

**Architecture:** Additive throughout. The old dashboard keeps working. Business
rules that more than one router needs live in `app/services/`, never inline in a
route — the wallet-eligibility rule already proved why. Hardcoded pricing moves
into tables that seed to today's exact numbers.

**Tech Stack:** FastAPI, SQLAlchemy, Postgres (prod) / SQLite (tests), pytest,
Razorpay, standalone `migrate_*.py` scripts (no Alembic).

**Prerequisite:** the branch `vps-migration-hardening` at `fabc398` or later
(step-3 work builds on `accepts_wallet`, `kiosk_type`, `onboarding_stage`).

---

## Context every task needs

**The business.** Three kinds of kiosk. `PLATFORM` — Printvendo owns and runs it,
money is ours. `SOLD` — the shop bought the hardware, uses their own Razorpay,
we earn a subscription. `SAAS` — the shop's own PC runs our agent, their
Razorpay, subscription. Wallet top-ups land in the platform account, so wallet
balance may only be spent at kiosks that resolve to platform keys.

**Owners are paid directly, so there are no settlements.** Every SOLD and SAAS
owner configures their own Razorpay keys as a condition of going live, and from
then on student money goes straight to them without passing through Printvendo.
This is the single most load-bearing rule in the system. It replaces the old
model, which collected on a keyless owner's behalf and settled up afterwards,
and which caused most of the old system's confusion.

Read it as "the platform never touches an owner's money", not "the platform
keeps it". Three consequences follow, and no task may violate them:

1. **An owner's earnings never enter the platform account.** There is no
   forwarding mechanism because none is needed.
2. **An owned kiosk must not take student money before its keys exist** — that
   is the single case where an owner's takings would land in the platform
   account, which is the thing this model exists to prevent. Hence the `KEYS`
   step before `LIVE` in `onboarding_stage`; see Task 14. The gate protects the
   owner.
3. **Wallet balance is spendable only at platform kiosks.** Not a policy choice
   — wallet top-ups are platform money and spending them elsewhere would put
   the platform in the middle of someone else's transaction.

Any code, comment or response text that says an owner "is paid through
settlements" is wrong and must be corrected wherever it is found. The existing
`Settlement` model and `/owner/settlements/*` endpoints stay only until the old
dashboard is retired; neither new app may call them.

**Money is rupees.** `Numeric(10,2)`. Never float arithmetic — see
`app/utils/money.py`.

**Roles** are three independent booleans on `User`: `is_admin` (platform owner,
`/owner/*` and `/admin/*`), `is_kiosk_owner` (`/kiosk/*`), `is_refiller`
(`/refiller/*`, paper only, no money). Owner and refiller logins are minted only
by an admin via `POST /owner/accounts`.

**Testing.** `python -m pytest -q` from `cloud-backend/`. 188 tests pass today.
`tests/conftest.py` provides the `db` fixture and `make_user`, `make_printer`,
`own`, `make_job`, `make_printer_job`. It imports models only, never routers.

**Commit trailer** — every commit in this plan ends with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01A4XWkubts2ivTaY4xjMUTA
```

---

## File structure

| File | Responsibility |
|---|---|
| `app/services/gateway_routing.py` | NEW — the single answer to "whose Razorpay takes the money at this kiosk" |
| `app/services/wallet_eligibility.py` | exists — rewritten to defer to `gateway_routing` |
| `app/models/subscription_plan.py` | NEW — `SubscriptionPlan`, `PlanDiscount` |
| `app/models/admin_audit_log.py` | NEW — `AdminAuditLog` |
| `app/services/plans.py` | NEW — plan lookup, duration discount maths, price limits |
| `app/services/audit.py` | NEW — `record_audit()` |
| `app/services/refunds.py` | NEW — the refund state machine, wallet and gateway |
| `app/schemas/pricing.py` | NEW — `PricingRead`, `PricingUpdate` |
| `app/schemas/plans.py` | NEW — plan and discount request/response shapes |
| `app/routers/kiosk.py` | + pricing, staff, refund, earnings |
| `app/routers/owner.py` | + plans CRUD, terms, onboarding, queue, audit, search |
| `migrate_add_plan_tables.py` | NEW — plans, discounts, subscription columns, audit log |
| `seed_subscription_plans.py` | NEW — seeds today's exact numbers |

Rules live in services because both `/kiosk/*` (owner) and `/owner/*` (admin)
perform the same operations against different scopes. A rule written inline in
one router is a rule the other router will get subtly wrong.

---

# Part 1 — Foundations

### Task 1: One answer for "whose gateway takes the money"

`payments.get_razorpay_client_for_printer` routes to the owner only when the
owner has **an active subscription AND configured keys**.
`wallet_eligibility.kiosk_accepts_wallet` checks only the keys.

Most of that gap is already closed elsewhere: `printers.py:210` hides a kiosk
entirely when its owner has `subscription_enabled` set and the subscription has
lapsed, so no student ever reaches it.

**The case that survives** is an owner who never opted in — `subscription_enabled`
is False — but has configured keys. That kiosk *is* listed, and
`get_razorpay_client_for_printer` falls back to the platform keys, so the
platform collects. Yet `kiosk_accepts_wallet` sees the keys and blocks the
wallet. It costs a student one payment method and risks no money, but two
functions answering the same question differently is how the money bug comes
back.

**Files:**
- Create: `app/services/gateway_routing.py`
- Modify: `app/services/wallet_eligibility.py`
- Test: `tests/test_gateway_routing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gateway_routing.py`:

```python
"""Whose Razorpay account takes the money at this kiosk?

One question, one answer. payments.get_razorpay_client_for_printer and
wallet_eligibility.kiosk_accepts_wallet both depend on it, and when they
disagreed the wallet was blocked at kiosks that in fact paid the platform.
"""
from app.models.kiosk_payment_config import KioskPaymentConfig
from app.models.subscription import Subscription
from app.services.gateway_routing import resolves_to_owner_gateway
from tests.conftest import make_printer, make_user, own


def _configure_keys(db, owner):
    db.add(
        KioskPaymentConfig(
            user_id=owner.id,
            razorpay_key_id="rzp_test_owner",
            razorpay_key_secret="secret",
            is_configured=True,
        )
    )
    db.commit()


def _activate_subscription(db, owner):
    owner.subscription_enabled = True
    db.add(Subscription(
        user_id=owner.id,
        plan_tier="PRO",
        monthly_price=1800,
        settlement_type="DIRECT",
        duration_months=1,
        total_amount=1800,
        status="ACTIVE",
    ))
    db.commit()


def test_unowned_kiosk_uses_platform(db):
    printer = make_printer(db)
    assert resolves_to_owner_gateway(db, printer) is False


def test_owner_without_keys_uses_platform(db):
    printer = make_printer(db)
    owner = make_user(db, email="a@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    _activate_subscription(db, owner)
    assert resolves_to_owner_gateway(db, printer) is False


def test_owner_with_keys_but_no_active_subscription_uses_platform(db):
    """This is the case the two old implementations disagreed about."""
    printer = make_printer(db)
    owner = make_user(db, email="b@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    _configure_keys(db, owner)
    assert resolves_to_owner_gateway(db, printer) is False


def test_owner_with_keys_and_active_subscription_uses_own_gateway(db):
    printer = make_printer(db)
    owner = make_user(db, email="c@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    _configure_keys(db, owner)
    _activate_subscription(db, owner)
    assert resolves_to_owner_gateway(db, printer) is True
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_gateway_routing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.gateway_routing'`

- [ ] **Step 3: Write the service**

Create `app/services/gateway_routing.py`:

```python
"""Whose Razorpay account collects money at a given kiosk.

Exactly one predicate, because at least three things depend on the answer:
who gets paid (payments.get_razorpay_client_for_printer), whether wallet
balance may be spent there (wallet_eligibility), and where a refund must be
issued from (services.refunds).

An owner collects into their own account only when BOTH are true:
  * they have an ACTIVE subscription, and
  * they have configured, non-empty Razorpay keys.

Either one missing and the platform's keys take the payment, because that is
what payments.py actually does. This function must keep mirroring that
routing decision — if the routing changes, change it here.
"""
from sqlalchemy.orm import Session

from app.models.kiosk_payment_config import KioskPaymentConfig
from app.models.printer import Printer
from app.models.printer_owner import PrinterOwner
from app.models.subscription import Subscription
from app.models.user import User


def owner_of(db: Session, printer: Printer) -> User | None:
    """The kiosk owner, or None for a platform-run kiosk."""
    ownership = (
        db.query(PrinterOwner).filter(PrinterOwner.printer_id == printer.id).first()
    )
    if ownership is None:
        return None
    return db.query(User).filter(User.id == ownership.user_id).first()


def resolves_to_owner_gateway(db: Session, printer: Printer) -> bool:
    """True when payments here land in the owner's Razorpay account."""
    owner = owner_of(db, printer)
    if owner is None or not owner.subscription_enabled:
        return False

    active = (
        db.query(Subscription)
        .filter(Subscription.user_id == owner.id, Subscription.status == "ACTIVE")
        .first()
    )
    if active is None:
        return False

    config = (
        db.query(KioskPaymentConfig)
        .filter(
            KioskPaymentConfig.user_id == owner.id,
            KioskPaymentConfig.is_configured == True,  # noqa: E712
        )
        .first()
    )
    return bool(config and config.razorpay_key_id and config.razorpay_key_secret)
```

- [ ] **Step 4: Point wallet eligibility at it**

Replace **the whole of** `app/services/wallet_eligibility.py`. The existing file
ends with the comment *"the owner is paid through settlements"*, which is now
false — there are no settlements — so the docstring goes too, not just the body:

```python
"""Decides whether student wallet balance may be spent at a kiosk.

Wallet top-ups are collected into the PLATFORM Razorpay account, and the
platform never pays that money onward to a kiosk owner — there are no
settlements. So wallet balance is spendable only where the platform itself
collects. Anywhere else, the student would pay Printvendo and the shop would
print for free with no way to ever be made whole.

The routing question — whose account collects here — has exactly one answer,
in gateway_routing. This module only decides what that answer means for the
wallet.
"""
from sqlalchemy.orm import Session

from app.models.printer import Printer
from app.services.gateway_routing import resolves_to_owner_gateway


def kiosk_accepts_wallet(db: Session, printer: Printer) -> bool:
    """True when payments at this kiosk land in the platform's account."""
    return not resolves_to_owner_gateway(db, printer)
```

- [ ] **Step 5: Run the existing wallet tests too**

Run: `python -m pytest tests/test_gateway_routing.py tests/test_wallet_eligibility.py -q`
Expected: `4 passed` from the new file and `6 passed` from the old one — **10 passed**.

`test_owner_with_configured_keys_rejects_wallet` in the old file configures keys
but no subscription. Under the corrected rule that kiosk now uses the PLATFORM
gateway, so wallet is **allowed** and the old assertion is wrong. Fix it by
giving that test an active subscription, matching `_activate_subscription`
above, so it still tests what its name says. Do not weaken the assertion.

- [ ] **Step 6: Run the whole suite and commit**

Run: `python -m pytest -q`
Expected: `192 passed`

```bash
git add app/services/gateway_routing.py app/services/wallet_eligibility.py tests/test_gateway_routing.py tests/test_wallet_eligibility.py
git commit -m "fix(wallet): match gateway routing exactly when deciding wallet eligibility

kiosk_accepts_wallet checked only for configured keys; payments.py also
requires an active subscription before routing to the owner. An owner with
keys and a lapsed subscription collects into the platform account, so wallet
should have been allowed there and was not."
```

---

### Task 2: Plan tables

**Files:**
- Create: `app/models/subscription_plan.py`
- Modify: `tests/conftest.py` (register the new models)
- Test: `tests/test_plans.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_plans.py`:

```python
"""Plans and their duration discounts live in the database, not in code.

subscription.py:61-81 hardcoded them, so changing a price meant a redeploy.
"""
from decimal import Decimal

from app.models.subscription_plan import PlanDiscount, SubscriptionPlan


def test_plan_round_trips(db):
    plan = SubscriptionPlan(
        name="Pro",
        monthly_price=Decimal("1800.00"),
        max_kiosks=5,
        price_floor_bw=Decimal("1.00"),
        price_ceiling_bw=Decimal("5.00"),
        price_floor_color=Decimal("5.00"),
        price_ceiling_color=Decimal("20.00"),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    assert plan.is_active is True
    assert plan.monthly_price == Decimal("1800.00")


def test_discounts_hang_off_a_plan(db):
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()

    db.add_all([
        PlanDiscount(plan_id=plan.id, duration_months=6, percent=Decimal("10")),
        PlanDiscount(plan_id=plan.id, duration_months=12, percent=Decimal("15")),
    ])
    db.commit()

    rows = db.query(PlanDiscount).filter(PlanDiscount.plan_id == plan.id).all()
    assert {r.duration_months: r.percent for r in rows} == {
        6: Decimal("10"),
        12: Decimal("15"),
    }
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_plans.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.subscription_plan'`

- [ ] **Step 3: Write the models**

Create `app/models/subscription_plan.py`:

```python
"""Subscription plans and their duration discounts.

Replaces the SUBSCRIPTION_PLANS and DURATION_DISCOUNTS dicts in
routers/subscription.py, which required a redeploy to change a price.

Price floors and ceilings bound what a kiosk owner may charge students for a
page. They live on the plan because they are a commercial term of the plan,
not a property of the machine.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.db.session import Base


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    monthly_price = Column(Numeric(10, 2), nullable=False)
    max_kiosks = Column(Integer, nullable=False, default=1)

    # Bounds on student-facing page prices at kiosks on this plan.
    # Null means unbounded on that side.
    price_floor_bw = Column(Numeric(10, 2), nullable=True)
    price_ceiling_bw = Column(Numeric(10, 2), nullable=True)
    price_floor_color = Column(Numeric(10, 2), nullable=True)
    price_ceiling_color = Column(Numeric(10, 2), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PlanDiscount(Base):
    """Percent off when a plan is bought for several months up front."""

    __tablename__ = "plan_discounts"
    __table_args__ = (UniqueConstraint("plan_id", "duration_months", name="uq_plan_duration"),)

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"), nullable=False, index=True)
    duration_months = Column(Integer, nullable=False)
    percent = Column(Numeric(5, 2), nullable=False, default=0)
```

- [ ] **Step 4: Register the models in conftest**

`tests/conftest.py` imports each model module so `create_all` sees the table.
Add beside the existing imports:

```python
import app.models.subscription_plan  # noqa: F401
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_plans.py -q`
Expected: `2 passed`

- [ ] **Step 6: Commit**

```bash
git add app/models/subscription_plan.py tests/conftest.py tests/test_plans.py
git commit -m "feat(plans): subscription_plans and plan_discounts tables"
```

---

### Task 3: Subscription columns for negotiated terms

You set a negotiated price and a free period per owner. Both are per-owner
overrides of the plan, so they belong on the subscription.

**Files:**
- Modify: `app/models/subscription.py`
- Test: `tests/test_plans.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plans.py`:

```python
from datetime import datetime, timedelta

from app.models.subscription import Subscription


def test_subscription_carries_negotiated_terms(db):
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()

    free_until = datetime.utcnow() + timedelta(days=90)
    sub = Subscription(
        user_id=1,
        plan_tier="PRO",
        plan_id=plan.id,
        monthly_price=Decimal("1800.00"),
        negotiated_price=Decimal("1200.00"),
        free_until=free_until,
        settlement_type="DIRECT",
        duration_months=1,
        total_amount=Decimal("1200.00"),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    assert sub.plan_id == plan.id
    assert sub.negotiated_price == Decimal("1200.00")
    assert sub.free_until is not None


def test_negotiated_terms_default_to_none(db):
    sub = Subscription(
        user_id=2,
        plan_tier="PRO",
        monthly_price=Decimal("1800.00"),
        settlement_type="DIRECT",
        duration_months=1,
        total_amount=Decimal("1800.00"),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    assert sub.plan_id is None
    assert sub.negotiated_price is None
    assert sub.free_until is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_plans.py -q`
Expected: FAIL — `TypeError: 'plan_id' is an invalid keyword argument for Subscription`

- [ ] **Step 3: Add the columns**

In `app/models/subscription.py`, after the `plan_tier` column:

```python
    # Set once plans moved into the database. Nullable because existing rows
    # predate the table and are identified by plan_tier alone.
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"), nullable=True, index=True)

    # Per-owner commercial overrides, both negotiated by hand:
    #   negotiated_price beats the plan's monthly_price for this owner
    #   free_until treats the subscription as active and unbilled until then
    negotiated_price = Column(Numeric(10, 2), nullable=True)
    free_until = Column(DateTime, nullable=True)
```

`ForeignKey` and `Numeric` are already imported in that file; confirm before
adding.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_plans.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add app/models/subscription.py tests/test_plans.py
git commit -m "feat(plans): negotiated price and free period on subscriptions"
```

---

### Task 4: Audit log

Every admin action that changes money, pricing or access writes a row. This is
what makes "who lowered this price" answerable six months from now.

**Files:**
- Create: `app/models/admin_audit_log.py`, `app/services/audit.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_audit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit.py`:

```python
"""Admin actions leave a trail, including what the value was before."""
from app.models.admin_audit_log import AdminAuditLog
from app.services.audit import record_audit
from tests.conftest import make_user


def test_records_before_and_after(db):
    actor = make_user(db, email="admin@test.in", is_admin=True)

    record_audit(
        db,
        actor_id=actor.id,
        action="pricing.update",
        entity_type="printer",
        entity_id=7,
        before={"bw_single_sided": 2.0},
        after={"bw_single_sided": 3.0},
        note="owner raised price",
    )
    db.commit()

    row = db.query(AdminAuditLog).one()
    assert row.actor_id == actor.id
    assert row.action == "pricing.update"
    assert row.entity_id == 7
    assert row.before == {"bw_single_sided": 2.0}
    assert row.after == {"bw_single_sided": 3.0}
    assert row.created_at is not None


def test_does_not_commit_on_its_own(db):
    """The caller owns the transaction, so a failed action logs nothing."""
    actor = make_user(db, email="admin2@test.in", is_admin=True)

    record_audit(db, actor_id=actor.id, action="x", entity_type="printer", entity_id=1)
    db.rollback()

    assert db.query(AdminAuditLog).count() == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_audit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.admin_audit_log'`

- [ ] **Step 3: Write the model**

Create `app/models/admin_audit_log.py`:

```python
"""A record of every consequential action taken through an admin surface.

before/after are JSON snapshots of only the fields that changed, not whole
rows — a diff is readable, a row dump is not.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.db.session import Base


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(Integer, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)   # e.g. "pricing.update"
    entity_type = Column(String, nullable=False)          # "printer", "owner", "plan"
    entity_id = Column(Integer, nullable=True, index=True)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
```

- [ ] **Step 4: Write the helper**

Create `app/services/audit.py`:

```python
"""Write an audit row.

Deliberately does NOT commit. The audit row and the change it describes must
land in the same transaction — otherwise a failed action leaves a log entry
claiming something happened that did not.
"""
from typing import Any

from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog


def record_audit(
    db: Session,
    *,
    actor_id: int,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    note: str | None = None,
) -> AdminAuditLog:
    row = AdminAuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        note=note,
    )
    db.add(row)
    return row
```

- [ ] **Step 5: Register in conftest**

Add to `tests/conftest.py`:

```python
import app.models.admin_audit_log  # noqa: F401
```

- [ ] **Step 6: Run the tests and commit**

Run: `python -m pytest tests/test_audit.py -q`
Expected: `2 passed`

```bash
git add app/models/admin_audit_log.py app/services/audit.py tests/conftest.py tests/test_audit.py
git commit -m "feat(audit): admin audit log with before/after snapshots"
```

---

### Task 5: Migration and seed

**Files:**
- Create: `migrate_add_plan_tables.py`, `seed_subscription_plans.py`

- [ ] **Step 1: Write the migration**

Create `migrate_add_plan_tables.py`:

```python
"""Create the plan, discount and audit tables; extend subscriptions.

Run manually, matching the migrate_*.py convention:

    python migrate_add_plan_tables.py

Additive and idempotent. Old code ignores the new tables and columns, so the
API does not need stopping first.

Run seed_subscription_plans.py afterwards — the tables are useless empty, and
the seed reproduces today's hardcoded numbers exactly.
"""
from sqlalchemy import inspect, text

from app.db.session import Base, engine

# Importing the modules registers the tables on Base.metadata.
import app.models.subscription_plan  # noqa: F401
import app.models.admin_audit_log  # noqa: F401

NEW_SUBSCRIPTION_COLUMNS_PG = [
    ("plan_id", "INTEGER"),
    ("negotiated_price", "NUMERIC(10,2)"),
    ("free_until", "TIMESTAMP"),
]


def run() -> None:
    # create_all only creates what is missing, so this is safe to re-run.
    Base.metadata.create_all(
        engine,
        tables=[
            Base.metadata.tables["subscription_plans"],
            Base.metadata.tables["plan_discounts"],
            Base.metadata.tables["admin_audit_log"],
        ],
    )

    existing = {c["name"] for c in inspect(engine).get_columns("subscriptions")}
    dialect = engine.dialect.name

    with engine.begin() as conn:
        for name, ddl in NEW_SUBSCRIPTION_COLUMNS_PG:
            if name in existing:
                continue
            if dialect == "postgresql":
                conn.execute(
                    text(f"ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS {name} {ddl}")
                )
            else:
                conn.execute(text(f"ALTER TABLE subscriptions ADD COLUMN {name} {ddl}"))

    print("subscription_plans, plan_discounts, admin_audit_log ensured")
    print("subscriptions: plan_id, negotiated_price, free_until ensured")


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Write the seed**

Create `seed_subscription_plans.py`:

```python
"""Seed subscription_plans from the values that were hardcoded in code.

The first run must produce numbers identical to today's, so nobody's bill
changes because plans moved into a table. Idempotent: a plan that already
exists by name is left exactly as it is, including any price you have since
edited in the console.

Price floors and ceilings are new — nothing bounded page prices before. The
values here are deliberately wide; tighten them in the console.
"""
from decimal import Decimal

from app.db.session import SessionLocal
from app.models.subscription_plan import PlanDiscount, SubscriptionPlan

PLANS = [
    {
        "name": "Pro",
        "monthly_price": Decimal("1800.00"),
        "max_kiosks": 5,
        "price_floor_bw": Decimal("1.00"),
        "price_ceiling_bw": Decimal("10.00"),
        "price_floor_color": Decimal("3.00"),
        "price_ceiling_color": Decimal("40.00"),
        "discounts": {1: Decimal("0"), 6: Decimal("10"), 12: Decimal("15")},
    },
]


def run() -> None:
    db = SessionLocal()
    try:
        for spec in PLANS:
            discounts = spec.pop("discounts")
            existing = (
                db.query(SubscriptionPlan)
                .filter(SubscriptionPlan.name == spec["name"])
                .first()
            )
            if existing:
                print(f"plan {spec['name']!r} already present (id={existing.id}) - left alone")
                plan = existing
            else:
                plan = SubscriptionPlan(**spec)
                db.add(plan)
                db.commit()
                db.refresh(plan)
                print(f"plan {plan.name!r} created (id={plan.id})")

            for months, percent in discounts.items():
                already = (
                    db.query(PlanDiscount)
                    .filter(
                        PlanDiscount.plan_id == plan.id,
                        PlanDiscount.duration_months == months,
                    )
                    .first()
                )
                if already:
                    continue
                db.add(PlanDiscount(plan_id=plan.id, duration_months=months, percent=percent))
            db.commit()
        print("done")
    finally:
        db.close()


if __name__ == "__main__":
    run()
```

- [ ] **Step 3: Verify both on a throwaway database, running each twice**

```bash
rm -f /tmp/plans.db
DATABASE_URL="sqlite:////tmp/plans.db" python -c "
from app.db.session import Base, engine
import app.models  # noqa
Base.metadata.create_all(engine)
print('base schema created')
"
DATABASE_URL="sqlite:////tmp/plans.db" python migrate_add_plan_tables.py
DATABASE_URL="sqlite:////tmp/plans.db" python migrate_add_plan_tables.py
DATABASE_URL="sqlite:////tmp/plans.db" python seed_subscription_plans.py
DATABASE_URL="sqlite:////tmp/plans.db" python seed_subscription_plans.py
```

Expected: no traceback on either second run, and the second seed prints
`plan 'Pro' already present`.

- [ ] **Step 4: Commit**

```bash
git add migrate_add_plan_tables.py seed_subscription_plans.py
git commit -m "feat(migration): plan tables, subscription terms, audit log"
```

---

### Task 6: Plan-driven pricing (bug B3)

`subscription.py` computes prices from module-level dicts. Move that to the
tables, proving the numbers do not change.

**Files:**
- Create: `app/services/plans.py`
- Modify: `app/routers/subscription.py`
- Test: `tests/test_plans.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plans.py`:

```python
import pytest

from app.services.plans import discount_percent_for, plan_total, resolve_plan


def _seed_pro(db):
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()
    db.add_all([
        PlanDiscount(plan_id=plan.id, duration_months=1, percent=Decimal("0")),
        PlanDiscount(plan_id=plan.id, duration_months=6, percent=Decimal("10")),
        PlanDiscount(plan_id=plan.id, duration_months=12, percent=Decimal("15")),
    ])
    db.commit()
    return plan


@pytest.mark.parametrize(
    "months,expected_total,expected_pct",
    [
        (1, Decimal("1800.00"), Decimal("0")),
        (6, Decimal("9720.00"), Decimal("10")),   # 10800 - 10%
        (12, Decimal("18360.00"), Decimal("15")), # 21600 - 15%
    ],
)
def test_totals_match_the_old_hardcoded_maths(db, months, expected_total, expected_pct):
    plan = _seed_pro(db)
    total, pct = plan_total(db, plan, months)
    assert total == expected_total
    assert pct == expected_pct


def test_unknown_duration_gets_no_discount(db):
    plan = _seed_pro(db)
    total, pct = plan_total(db, plan, 3)
    assert pct == Decimal("0")
    assert total == Decimal("5400.00")


def test_negotiated_price_replaces_the_plan_price(db):
    plan = _seed_pro(db)
    total, pct = plan_total(db, plan, 6, negotiated_price=Decimal("1200.00"))
    assert total == Decimal("6480.00")  # 7200 - 10%


def test_resolve_plan_falls_back_to_the_only_active_plan(db):
    plan = _seed_pro(db)
    assert resolve_plan(db, plan_id=None, plan_tier="PRO").id == plan.id


def test_discount_lookup_is_empty_without_rows(db):
    plan = SubscriptionPlan(name="Bare", monthly_price=Decimal("100.00"), max_kiosks=1)
    db.add(plan)
    db.commit()
    assert discount_percent_for(db, plan, 12) == Decimal("0")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_plans.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.plans'`

- [ ] **Step 3: Write the service**

Create `app/services/plans.py`:

```python
"""Plan lookup and subscription price maths.

Replaces SUBSCRIPTION_PLANS / DURATION_DISCOUNTS / _calculate_total in
routers/subscription.py. The arithmetic is deliberately identical, including
the final quantize to paise, so moving plans into the database changes no
existing bill.
"""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.subscription_plan import PlanDiscount, SubscriptionPlan


def resolve_plan(
    db: Session, *, plan_id: int | None = None, plan_tier: str | None = None
) -> SubscriptionPlan | None:
    """Find the plan by id, else by name, else the single active plan.

    The name fallback exists because subscriptions predating the table
    identify their plan by plan_tier ("PRO") alone.
    """
    if plan_id is not None:
        return db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()

    query = db.query(SubscriptionPlan).filter(SubscriptionPlan.is_active == True)  # noqa: E712
    if plan_tier:
        by_name = query.filter(SubscriptionPlan.name.ilike(plan_tier)).first()
        if by_name:
            return by_name

    active = query.all()
    return active[0] if len(active) == 1 else None


def discount_percent_for(db: Session, plan: SubscriptionPlan, duration_months: int) -> Decimal:
    """Percent off for that duration. An unlisted duration gets nothing."""
    row = (
        db.query(PlanDiscount)
        .filter(
            PlanDiscount.plan_id == plan.id,
            PlanDiscount.duration_months == duration_months,
        )
        .first()
    )
    return Decimal(row.percent) if row else Decimal("0")


def plan_total(
    db: Session,
    plan: SubscriptionPlan,
    duration_months: int,
    *,
    negotiated_price: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """Return (total_after_discount, discount_percent).

    negotiated_price, when set, replaces the plan's monthly price for this
    owner. The duration discount still applies on top — a negotiated rate and
    a long commitment are separate concessions.
    """
    monthly = Decimal(negotiated_price if negotiated_price is not None else plan.monthly_price)
    percent = discount_percent_for(db, plan, duration_months)
    raw = monthly * duration_months
    total = raw - (raw * percent / Decimal("100"))
    return total.quantize(Decimal("0.01")), percent
```

- [ ] **Step 4: Point the router at it**

In `app/routers/subscription.py`:

1. Keep `SUBSCRIPTION_PLANS` and `DURATION_DISCOUNTS` where they are but mark
   them as the seed source, so nothing that still reads them breaks:

```python
# Superseded by the subscription_plans table (see app/services/plans.py and
# seed_subscription_plans.py). Kept only as the seed's reference values and as
# a fallback when the table has not been seeded yet. Do not add plans here.
```

2. Replace the body of `_calculate_total` so it reads the table when it can:

```python
def _calculate_total(
    monthly_price: Decimal,
    duration_months: int,
    db: Session | None = None,
    plan: "SubscriptionPlan | None" = None,
) -> tuple[Decimal, Decimal]:
    """Return (total_amount, discount_percent) after the duration discount.

    Reads plan_discounts when a plan is supplied; falls back to the legacy
    DURATION_DISCOUNTS dict when the table has not been seeded, so an
    un-migrated database keeps charging the right amount.
    """
    if db is not None and plan is not None:
        return plan_total(db, plan, duration_months, negotiated_price=monthly_price)

    discount_pct = DURATION_DISCOUNTS.get(duration_months, Decimal("0"))
    raw_total = monthly_price * duration_months
    total = raw_total - (raw_total * discount_pct / Decimal("100"))
    return total.quantize(Decimal("0.01")), discount_pct
```

Add the imports at the top of the file:

```python
from app.models.subscription_plan import SubscriptionPlan
from app.services.plans import plan_total, resolve_plan
```

3. At each existing `_calculate_total(...)` call site, resolve the plan first
   and pass it through. Find them with:

```bash
grep -n "_calculate_total(" app/routers/subscription.py
```

For each call, insert before it:

```python
    plan = resolve_plan(db, plan_id=getattr(subscription, "plan_id", None), plan_tier=plan_tier)
```

and add `db=db, plan=plan` to the call. Use the `plan_tier` variable already in
scope at that point; if the call site has a subscription object instead, use
`subscription.plan_tier`.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_plans.py -q`
Expected: `10 passed`

- [ ] **Step 6: Run the whole suite and commit**

Run: `python -m pytest -q`
Expected: `202 passed`

Any existing subscription test that fails here means the maths changed —
investigate rather than adjusting the expected number.

```bash
git add app/services/plans.py app/routers/subscription.py tests/test_plans.py
git commit -m "fix(plans): read subscription pricing from the database

Closes B3. Prices and duration discounts were module-level dicts, so a price
change needed a redeploy. Falls back to the old dicts when the table is not
seeded, so the maths is unchanged either way."
```

---

# Part 2 — Owner endpoints

### Task 7: Price limits

An owner may change their page prices, but only inside the band their plan
allows. The band lives on the plan (Task 2); this is the check.

**Files:**
- Modify: `app/services/plans.py`
- Test: `tests/test_price_limits.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_price_limits.py`:

```python
"""A kiosk owner sets their own prices, within the band their plan allows.

Too low and a shop undercuts the network into unsustainability; too high and
students stop trusting the app. Null on either side means unbounded.
"""
from decimal import Decimal

import pytest

from app.models.subscription_plan import SubscriptionPlan
from app.services.plans import PriceOutOfRange, assert_prices_within_plan


def _plan(db, **kw):
    defaults = dict(
        name="Pro",
        monthly_price=Decimal("1800.00"),
        max_kiosks=5,
        price_floor_bw=Decimal("1.00"),
        price_ceiling_bw=Decimal("10.00"),
        price_floor_color=Decimal("3.00"),
        price_ceiling_color=Decimal("40.00"),
    )
    defaults.update(kw)
    plan = SubscriptionPlan(**defaults)
    db.add(plan)
    db.commit()
    return plan


def test_prices_inside_the_band_pass(db):
    plan = _plan(db)
    assert_prices_within_plan(plan, {"bw_single_sided": Decimal("2.00")})


def test_price_below_the_floor_is_rejected(db):
    plan = _plan(db)
    with pytest.raises(PriceOutOfRange) as exc:
        assert_prices_within_plan(plan, {"bw_single_sided": Decimal("0.50")})
    assert "bw_single_sided" in str(exc.value)


def test_price_above_the_ceiling_is_rejected(db):
    plan = _plan(db)
    with pytest.raises(PriceOutOfRange):
        assert_prices_within_plan(plan, {"color_single_sided": Decimal("99.00")})


def test_colour_prices_use_the_colour_band(db):
    """4.00 is fine for colour and would be fine for b/w — the bands differ."""
    plan = _plan(db)
    assert_prices_within_plan(plan, {"color_double_sided": Decimal("4.00")})
    with pytest.raises(PriceOutOfRange):
        assert_prices_within_plan(plan, {"color_double_sided": Decimal("2.00")})


def test_a_plan_with_no_band_allows_anything(db):
    plan = _plan(
        db,
        price_floor_bw=None,
        price_ceiling_bw=None,
        price_floor_color=None,
        price_ceiling_color=None,
    )
    assert_prices_within_plan(plan, {"bw_single_sided": Decimal("999.00")})


def test_no_plan_allows_anything(db):
    """A platform kiosk has no owner and therefore no plan."""
    assert_prices_within_plan(None, {"bw_single_sided": Decimal("999.00")})


def test_unset_fields_are_ignored(db):
    plan = _plan(db)
    assert_prices_within_plan(plan, {"bw_single_sided": None})
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_price_limits.py -q`
Expected: FAIL — `ImportError: cannot import name 'PriceOutOfRange'`

- [ ] **Step 3: Add the check to `app/services/plans.py`**

Append:

```python
COLOUR_FIELDS = ("color_single_sided", "color_double_sided")
BW_FIELDS = ("bw_single_sided", "bw_double_sided")


class PriceOutOfRange(ValueError):
    """A requested page price falls outside the plan's allowed band."""


def _band(plan: SubscriptionPlan, field: str) -> tuple[Decimal | None, Decimal | None]:
    if field in COLOUR_FIELDS:
        return plan.price_floor_color, plan.price_ceiling_color
    return plan.price_floor_bw, plan.price_ceiling_bw


def assert_prices_within_plan(
    plan: SubscriptionPlan | None, prices: dict[str, Decimal | None]
) -> None:
    """Raise PriceOutOfRange if any supplied price is outside the plan band.

    A missing plan, a missing bound, or a None price means "no opinion" — a
    platform kiosk has no owner and therefore no plan, and a plan may leave
    either side of a band open.
    """
    if plan is None:
        return

    for field, value in prices.items():
        if value is None:
            continue
        floor, ceiling = _band(plan, field)
        amount = Decimal(str(value))
        if floor is not None and amount < Decimal(floor):
            raise PriceOutOfRange(
                f"{field} must be at least {Decimal(floor):.2f} on the {plan.name} plan"
            )
        if ceiling is not None and amount > Decimal(ceiling):
            raise PriceOutOfRange(
                f"{field} must be at most {Decimal(ceiling):.2f} on the {plan.name} plan"
            )
```

- [ ] **Step 4: Run the tests and commit**

Run: `python -m pytest tests/test_price_limits.py -q`
Expected: `7 passed`

```bash
git add app/services/plans.py tests/test_price_limits.py
git commit -m "feat(plans): bound kiosk page prices by the owner's plan"
```

---

### Task 8: Read and update kiosk pricing

There is no pricing update endpoint today at all — prices are set once at kiosk
creation (`kiosk.py:324`) and never changeable afterwards.

**Files:**
- Create: `app/schemas/pricing.py`
- Modify: `app/routers/kiosk.py`
- Test: `tests/test_pricing_endpoint.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pricing_endpoint.py`:

```python
"""Owners set their own page prices, inside the plan's band, with an audit row."""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.admin_audit_log import AdminAuditLog
from app.models.subscription import Subscription
from app.models.subscription_plan import SubscriptionPlan
from app.routers.kiosk import kiosk_get_pricing, kiosk_set_pricing
from app.schemas.pricing import PricingUpdate
from tests.conftest import make_printer, make_user, own


def _owner_on_plan(db, printer, email="ko@test.in"):
    owner = make_user(db, email=email, is_kiosk_owner=True)
    own(db, owner, printer)
    plan = SubscriptionPlan(
        name="Pro",
        monthly_price=Decimal("1800.00"),
        max_kiosks=5,
        price_floor_bw=Decimal("1.00"),
        price_ceiling_bw=Decimal("10.00"),
        price_floor_color=Decimal("3.00"),
        price_ceiling_color=Decimal("40.00"),
    )
    db.add(plan)
    db.commit()
    db.add(Subscription(
        user_id=owner.id,
        plan_tier="Pro",
        plan_id=plan.id,
        monthly_price=Decimal("1800.00"),
        settlement_type="DIRECT",
        duration_months=1,
        total_amount=Decimal("1800.00"),
        status="ACTIVE",
    ))
    db.commit()
    return owner, plan


def test_reads_current_prices_and_the_band(db):
    printer = make_printer(db, bw_single_sided=2.0, color_single_sided=8.0)
    owner, _ = _owner_on_plan(db, printer)

    out = kiosk_get_pricing(printer_id=printer.id, db=db, current_user=owner)

    assert out["bw_single_sided"] == 2.0
    assert out["limits"]["bw"] == {"floor": 1.0, "ceiling": 10.0}
    assert out["limits"]["color"] == {"floor": 3.0, "ceiling": 40.0}


def test_updates_only_the_fields_supplied(db):
    printer = make_printer(db, bw_single_sided=2.0, color_single_sided=8.0)
    owner, _ = _owner_on_plan(db, printer)

    kiosk_set_pricing(
        printer_id=printer.id,
        payload=PricingUpdate(bw_single_sided=Decimal("3.00")),
        db=db,
        current_user=owner,
    )

    db.refresh(printer)
    assert float(printer.bw_single_sided) == 3.0
    assert float(printer.color_single_sided) == 8.0  # untouched


def test_rejects_a_price_outside_the_plan_band(db):
    printer = make_printer(db, bw_single_sided=2.0)
    owner, _ = _owner_on_plan(db, printer)

    with pytest.raises(HTTPException) as exc:
        kiosk_set_pricing(
            printer_id=printer.id,
            payload=PricingUpdate(bw_single_sided=Decimal("50.00")),
            db=db,
            current_user=owner,
        )
    assert exc.value.status_code == 400
    assert "at most" in str(exc.value.detail)

    db.refresh(printer)
    assert float(printer.bw_single_sided) == 2.0  # nothing written


def test_rejects_someone_elses_kiosk(db):
    printer = make_printer(db)
    _owner_on_plan(db, printer)
    stranger = make_user(db, email="other@test.in", is_kiosk_owner=True)

    with pytest.raises(HTTPException) as exc:
        kiosk_set_pricing(
            printer_id=printer.id,
            payload=PricingUpdate(bw_single_sided=Decimal("2.00")),
            db=db,
            current_user=stranger,
        )
    assert exc.value.status_code == 403


def test_rejects_an_empty_update(db):
    printer = make_printer(db)
    owner, _ = _owner_on_plan(db, printer)

    with pytest.raises(HTTPException) as exc:
        kiosk_set_pricing(
            printer_id=printer.id, payload=PricingUpdate(), db=db, current_user=owner
        )
    assert exc.value.status_code == 400


def test_writes_an_audit_row_with_the_old_price(db):
    printer = make_printer(db, bw_single_sided=2.0)
    owner, _ = _owner_on_plan(db, printer)

    kiosk_set_pricing(
        printer_id=printer.id,
        payload=PricingUpdate(bw_single_sided=Decimal("4.00")),
        db=db,
        current_user=owner,
    )

    row = db.query(AdminAuditLog).filter(AdminAuditLog.action == "pricing.update").one()
    assert row.actor_id == owner.id
    assert row.entity_id == printer.id
    assert row.before["bw_single_sided"] == 2.0
    assert row.after["bw_single_sided"] == 4.0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_pricing_endpoint.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.pricing'`

- [ ] **Step 3: Write the schema**

Create `app/schemas/pricing.py`:

```python
from decimal import Decimal

from pydantic import BaseModel, Field

PRICE_FIELDS = (
    "bw_single_sided",
    "bw_double_sided",
    "color_single_sided",
    "color_double_sided",
)


class PricingUpdate(BaseModel):
    """Per-page prices in rupees. Every field optional — omitted means unchanged.

    The plan's band is what actually bounds these; ge/le here only stop
    nonsense reaching the service.
    """

    bw_single_sided: Decimal | None = Field(default=None, ge=0, le=1000)
    bw_double_sided: Decimal | None = Field(default=None, ge=0, le=1000)
    color_single_sided: Decimal | None = Field(default=None, ge=0, le=1000)
    color_double_sided: Decimal | None = Field(default=None, ge=0, le=1000)

    def supplied(self) -> dict[str, Decimal]:
        """Only the fields the caller actually sent."""
        return {f: getattr(self, f) for f in PRICE_FIELDS if getattr(self, f) is not None}
```

- [ ] **Step 4: Add both endpoints**

In `app/routers/kiosk.py`, add the imports:

```python
from app.models.subscription import Subscription
from app.schemas.pricing import PRICE_FIELDS, PricingUpdate
from app.services.audit import record_audit
from app.services.plans import PriceOutOfRange, assert_prices_within_plan, resolve_plan
```

(Some may already be imported — check first.)

Add this helper near the top of the file if the module has no equivalent:

```python
def _as_float(value) -> float | None:
    """Prices are Numeric in the DB and JSON in the response."""
    return None if value is None else float(value)
```

Then both endpoints, beside the other `/kiosk/printers/{printer_id}/...` routes:

```python
def _plan_for_owner(db: Session, user: User):
    """The plan bounding this owner's prices, or None if they have no active one."""
    sub = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == "ACTIVE")
        .first()
    )
    if sub is None:
        return None
    return resolve_plan(db, plan_id=sub.plan_id, plan_tier=sub.plan_tier)


def _band_dict(plan, floor_attr: str, ceiling_attr: str) -> dict[str, float | None]:
    if plan is None:
        return {"floor": None, "ceiling": None}
    floor = getattr(plan, floor_attr)
    ceiling = getattr(plan, ceiling_attr)
    return {
        "floor": float(floor) if floor is not None else None,
        "ceiling": float(ceiling) if ceiling is not None else None,
    }


@router.get("/kiosk/printers/{printer_id}/pricing")
def kiosk_get_pricing(
    printer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> dict[str, Any]:
    """Current page prices plus the band the owner's plan allows.

    The band ships with the prices so the UI can show its limits without a
    second call and without hardcoding them.
    """
    printer = _assert_owns_printer(db, current_user, printer_id)
    plan = _plan_for_owner(db, current_user)

    out: dict[str, Any] = {f: _as_float(getattr(printer, f)) for f in PRICE_FIELDS}
    out["limits"] = {
        "bw": _band_dict(plan, "price_floor_bw", "price_ceiling_bw"),
        "color": _band_dict(plan, "price_floor_color", "price_ceiling_color"),
    }
    out["plan_name"] = plan.name if plan else None
    return out


@router.put("/kiosk/printers/{printer_id}/pricing")
def kiosk_set_pricing(
    printer_id: int,
    payload: PricingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> dict[str, Any]:
    """Change page prices, within the plan's band.

    Validates every supplied price before writing any of them, so a rejected
    update leaves prices exactly as they were rather than half-applied.
    """
    printer = _assert_owns_printer(db, current_user, printer_id)
    changes = payload.supplied()
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Send at least one price to change",
        )

    plan = _plan_for_owner(db, current_user)
    try:
        assert_prices_within_plan(plan, changes)
    except PriceOutOfRange as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    before = {f: _as_float(getattr(printer, f)) for f in changes}
    for field, value in changes.items():
        setattr(printer, field, value)
    after = {f: _as_float(getattr(printer, f)) for f in changes}

    record_audit(
        db,
        actor_id=current_user.id,
        action="pricing.update",
        entity_type="printer",
        entity_id=printer.id,
        before=before,
        after=after,
    )
    db.commit()
    db.refresh(printer)

    return {
        "status": "ok",
        "printer_id": printer.id,
        **{f: _as_float(getattr(printer, f)) for f in PRICE_FIELDS},
    }
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_pricing_endpoint.py -q`
Expected: `6 passed`

- [ ] **Step 6: Run the whole suite and commit**

Run: `python -m pytest -q`
Expected: `215 passed`

```bash
git add app/schemas/pricing.py app/routers/kiosk.py tests/test_pricing_endpoint.py
git commit -m "feat(pricing): owners can change page prices within their plan band

There was no pricing update endpoint at all — prices were set once when the
kiosk was created and never changeable."
```

---

### Task 9: Refunds

**The rules, which the tests pin:**

| Job was paid by | Kiosk collects into | `destination: "wallet"` | `destination: "source"` |
|---|---|---|---|
| Wallet | platform (always) | yes — instant credit | no: there is no gateway payment to reverse |
| Gateway | platform | yes | yes — Razorpay refund on platform keys |
| Gateway | owner's own account | **no** | yes — Razorpay refund on the owner's keys |

The forbidden cell is the important one. Crediting a student's wallet for money
sitting in a shop's Razorpay account would have the platform pay out cash it
never received — and with no settlements, nothing would ever recover it. That is
bug B1 in reverse.

**Files:**
- Create: `app/services/refunds.py`, `app/schemas/refund.py`
- Modify: `app/routers/kiosk.py`
- Test: `tests/test_refunds.py`

- [ ] **Step 1: Check the test helpers exist**

Run: `grep -n "^def make" tests/conftest.py`

The test below uses `make_job` and `make_printer_job`. If either has a different
name or signature, use the real one — do not invent a helper.

- [ ] **Step 2: Write the failing test**

Create `tests/test_refunds.py`:

```python
"""Refunding a job puts the money back where it can actually go.

Money in the platform's account can go to the student's wallet or back to
their card. Money in a shop's own Razorpay account can only go back to the
card — crediting the platform wallet would mean paying out cash the platform
never received, and there are no settlements to recover it.
"""
from decimal import Decimal

import pytest

from app.models.payment import Payment
from app.models.wallet import Wallet, WalletLedger
from app.services.refunds import RefundNotAllowed, refund_printer_job
from tests.conftest import make_job, make_printer, make_printer_job, make_user, own


class FakeRazorpay:
    """Stands in for razorpay.Client. Records what it was asked to refund."""

    def __init__(self):
        self.calls = []
        self.payment = self

    def refund(self, payment_id, data):
        self.calls.append((payment_id, data))
        return {"id": "rfnd_test_1", "status": "processed"}


def _paid_gateway_job(db, printer, student, amount="20.00"):
    job = make_job(db, student)
    pj = make_printer_job(db, job, printer, status="PRINTED")
    payment = Payment(
        job_id=job.id,
        user_id=student.id,
        printer_id=printer.id,
        amount=Decimal(amount),
        status="PAID",
        razorpay_payment_id="pay_test_123",
    )
    db.add(payment)
    db.commit()
    return pj, payment


def _paid_wallet_job(db, printer, student, amount="20.00"):
    job = make_job(db, student)
    pj = make_printer_job(db, job, printer, status="PRINTED")
    payment = Payment(
        job_id=job.id,
        user_id=student.id,
        printer_id=printer.id,
        amount=Decimal(amount),
        status="PAID",
        razorpay_payment_id=None,
    )
    db.add(payment)
    db.add(Wallet(user_id=student.id, balance=Decimal("0.00")))
    db.commit()
    return pj, payment


def _make_owner_key_kiosk(db, printer, email="ko@test.in"):
    """An owner whose own Razorpay collects: active subscription plus keys."""
    from app.models.kiosk_payment_config import KioskPaymentConfig
    from app.models.subscription import Subscription

    owner = make_user(db, email=email, is_kiosk_owner=True)
    own(db, owner, printer)
    owner.subscription_enabled = True
    db.add(Subscription(
        user_id=owner.id, plan_tier="Pro", monthly_price=Decimal("1800.00"),
        settlement_type="DIRECT", duration_months=1,
        total_amount=Decimal("1800.00"), status="ACTIVE",
    ))
    db.add(KioskPaymentConfig(
        user_id=owner.id, razorpay_key_id="k", razorpay_key_secret="s", is_configured=True
    ))
    db.commit()
    return owner


def test_gateway_refund_to_source_calls_razorpay_in_paise(db):
    printer = make_printer(db)
    student = make_user(db, email="s@test.in")
    pj, payment = _paid_gateway_job(db, printer, student, "20.00")
    gateway = FakeRazorpay()

    out = refund_printer_job(
        db, pj, destination="source", actor_user_id=1, gateway=gateway
    )

    assert gateway.calls == [("pay_test_123", {"amount": 2000, "speed": "normal"})]
    assert out["destination"] == "source"
    db.refresh(payment)
    assert payment.status == "REFUNDED"


def test_gateway_refund_to_wallet_credits_the_student(db):
    printer = make_printer(db)
    student = make_user(db, email="s2@test.in")
    db.add(Wallet(user_id=student.id, balance=Decimal("5.00")))
    db.commit()
    pj, payment = _paid_gateway_job(db, printer, student, "20.00")

    refund_printer_job(db, pj, destination="wallet", actor_user_id=1, gateway=None)

    wallet = db.query(Wallet).filter(Wallet.user_id == student.id).one()
    assert wallet.balance == Decimal("25.00")
    entry = db.query(WalletLedger).filter(WalletLedger.entry_type == "REFUND").one()
    assert entry.status == "SUCCESS"
    assert entry.amount == Decimal("20.00")


def test_wallet_paid_job_cannot_refund_to_source(db):
    printer = make_printer(db)
    student = make_user(db, email="s3@test.in")
    pj, _ = _paid_wallet_job(db, printer, student)

    with pytest.raises(RefundNotAllowed):
        refund_printer_job(db, pj, destination="source", actor_user_id=1, gateway=None)


def test_owner_gateway_job_cannot_refund_to_wallet(db):
    """The money is in the shop's account; the platform must not pay it out."""
    printer = make_printer(db)
    _make_owner_key_kiosk(db, printer)
    student = make_user(db, email="s4@test.in")
    pj, _ = _paid_gateway_job(db, printer, student)

    with pytest.raises(RefundNotAllowed) as exc:
        refund_printer_job(db, pj, destination="wallet", actor_user_id=1, gateway=None)
    assert "wallet" in str(exc.value).lower()


def test_refunding_twice_is_refused(db):
    printer = make_printer(db)
    student = make_user(db, email="s5@test.in")
    pj, _ = _paid_gateway_job(db, printer, student)
    gateway = FakeRazorpay()

    refund_printer_job(db, pj, destination="source", actor_user_id=1, gateway=gateway)
    with pytest.raises(RefundNotAllowed):
        refund_printer_job(db, pj, destination="source", actor_user_id=1, gateway=gateway)

    assert len(gateway.calls) == 1  # no second call to Razorpay


def test_refund_fails_the_printer_job(db):
    printer = make_printer(db)
    student = make_user(db, email="s6@test.in")
    pj, _ = _paid_gateway_job(db, printer, student)

    refund_printer_job(db, pj, destination="wallet", actor_user_id=1, gateway=None)

    db.refresh(pj)
    assert pj.status == "FAILED"
    assert pj.error_code == "REFUNDED"
```

- [ ] **Step 3: Run it and watch it fail**

Run: `python -m pytest tests/test_refunds.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.refunds'`

- [ ] **Step 4: Write the service**

Create `app/services/refunds.py`:

```python
"""Reverse a paid print job.

Two destinations, and which are legal depends on where the money actually is:

  * wallet  — credit the student's Printvendo wallet, instantly. Only legal
              when the platform holds the money, because the platform is what
              pays a wallet balance out.
  * source  — reverse the original Razorpay payment back to the card or UPI
              the student used, on whichever key took it. Takes days, and is
              the only option when a shop's own account holds the money.

Wallet-paid jobs can only go back to the wallet: there is no gateway payment
to reverse.

The gateway client is injected rather than constructed here so tests can prove
the exact call without touching Razorpay.
"""
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.printer import Printer
from app.models.printer_job import PrinterJob
from app.models.wallet import Wallet, WalletLedger
from app.services.audit import record_audit
from app.services.gateway_routing import resolves_to_owner_gateway
from app.utils.money import as_money

RefundDestination = Literal["wallet", "source"]


class RefundNotAllowed(ValueError):
    """This job cannot be refunded to this destination."""


def _payment_for(db: Session, printer_job: PrinterJob) -> Payment | None:
    return (
        db.query(Payment)
        .filter(
            Payment.job_id == printer_job.job_id,
            Payment.printer_id == printer_job.printer_id,
            Payment.status == "PAID",
        )
        .first()
    )


def _credit_wallet(db: Session, user_id: int, amount: Decimal, printer_job_id: int) -> None:
    wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if wallet is None:
        wallet = Wallet(user_id=user_id, balance=Decimal("0.00"))
        db.add(wallet)
        db.flush()

    db.add(
        WalletLedger(
            user_id=user_id,
            amount=amount,
            entry_type="REFUND",
            status="SUCCESS",
            reference_type="REFUND",
            reference_id=str(printer_job_id),
            note="Refund for print job",
        )
    )
    wallet.balance = as_money((wallet.balance or Decimal("0.00")) + amount)


def refund_printer_job(
    db: Session,
    printer_job: PrinterJob,
    *,
    destination: RefundDestination,
    actor_user_id: int,
    gateway: Any | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Refund one printer job. Commits. Returns a summary.

    Raises RefundNotAllowed for an already-refunded job, an unpaid job, or a
    destination the money cannot legally reach.
    """
    payment = _payment_for(db, printer_job)
    if payment is None:
        raise RefundNotAllowed("No captured payment found for this job")

    printer = db.query(Printer).filter(Printer.id == printer_job.printer_id).first()
    paid_by_gateway = bool(payment.razorpay_payment_id)
    amount = Decimal(payment.amount)

    if destination == "source":
        if not paid_by_gateway:
            raise RefundNotAllowed(
                "This job was paid from the wallet, so it can only be refunded to the wallet"
            )
        if gateway is None:
            raise RefundNotAllowed("No payment gateway available to reverse this payment")
        # Razorpay works in paise.
        gateway.payment.refund(
            payment.razorpay_payment_id,
            {"amount": int((amount * 100).to_integral_value()), "speed": "normal"},
        )
    else:
        if paid_by_gateway and printer is not None and resolves_to_owner_gateway(db, printer):
            raise RefundNotAllowed(
                "This payment went to the shop's own account, so it cannot be "
                "refunded to the wallet. Refund it to the original payment method."
            )
        _credit_wallet(db, payment.user_id, amount, printer_job.id)

    payment.status = "REFUNDED"
    printer_job.status = "FAILED"
    printer_job.error_code = "REFUNDED"
    printer_job.error_message = "Refunded"

    record_audit(
        db,
        actor_id=actor_user_id,
        action="job.refund",
        entity_type="printer_job",
        entity_id=printer_job.id,
        before={"payment_status": "PAID"},
        after={"payment_status": "REFUNDED", "destination": destination},
        note=note,
    )
    db.commit()

    return {
        "status": "ok",
        "printer_job_id": printer_job.id,
        "amount": float(amount),
        "destination": destination,
    }
```

- [ ] **Step 5: Write the schema and endpoint**

Create `app/schemas/refund.py`:

```python
from typing import Literal

from pydantic import BaseModel, Field


class RefundRequest(BaseModel):
    """Where the money should go back to.

    Defaults to "source" because returning money the way it arrived is what a
    student expects, and it is legal for every payment type except wallet-paid.
    """

    destination: Literal["wallet", "source"] = "source"
    note: str | None = Field(default=None, max_length=200)
```

Before writing the endpoint, check for a circular import:

```bash
grep -n "^from app.routers\|^import app.routers" app/routers/payments.py
```

If `payments.py` imports from `kiosk.py`, do **not** import
`get_razorpay_client_for_printer` into `kiosk.py`. Move that function into
`app/services/gateway_routing.py` instead and have both routers import it from
there — it belongs with the routing rule anyway.

Then in `app/routers/kiosk.py`:

```python
from app.schemas.refund import RefundRequest
from app.services.refunds import RefundNotAllowed, refund_printer_job


@router.post("/kiosk/printers/{printer_id}/jobs/{printer_job_id}/refund")
def kiosk_refund_job(
    printer_id: int,
    printer_job_id: int,
    payload: RefundRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> dict[str, Any]:
    """Refund a job on one of your own kiosks.

    Refunds to the original payment method go out on whichever Razorpay keys
    took the money, so a shop refunds from its own account and the platform
    from its own. There are no settlements, so money never crosses between
    them.
    """
    printer = _assert_owns_printer(db, current_user, printer_id)
    printer_job = (
        db.query(PrinterJob)
        .filter(PrinterJob.id == printer_job_id, PrinterJob.printer_id == printer.id)
        .first()
    )
    if printer_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    gateway = None
    if payload.destination == "source":
        gateway, _key_id = get_razorpay_client_for_printer(db, printer)

    try:
        return refund_printer_job(
            db,
            printer_job,
            destination=payload.destination,
            actor_user_id=current_user.id,
            gateway=gateway,
            note=payload.note,
        )
    except RefundNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_refunds.py -q`
Expected: `6 passed`

- [ ] **Step 7: Run the whole suite and commit**

Run: `python -m pytest -q`
Expected: `221 passed`

```bash
git add app/services/refunds.py app/schemas/refund.py app/routers/kiosk.py tests/test_refunds.py
git commit -m "feat(refunds): owner-scoped refunds to wallet or original method

Refunds to source go out on whichever Razorpay keys took the payment. A
gateway payment held in a shop's own account may not be refunded to the
platform wallet — that would pay out cash the platform never received, and
there are no settlements to recover it."
```

---

### Task 10: Staff

Refiller accounts are minted only by you (`accounts.py:109`), and that stays.
This lets an owner assign an existing refiller to their own kiosks and take them
off again — which today only an admin can do (`accounts.py:211`).

**Files:**
- Modify: `app/routers/kiosk.py`
- Test: `tests/test_kiosk_staff.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_kiosk_staff.py`:

```python
"""An owner decides who refills their kiosks; only you decide who exists.

Creating a refiller login stays admin-only (accounts.py). This is assignment
of an existing refiller to kiosks the owner actually owns.
"""
import pytest
from fastapi import HTTPException

from app.models.printer_refiller import PrinterRefiller
from app.routers.kiosk import kiosk_assign_staff, kiosk_list_staff, kiosk_unassign_staff
from tests.conftest import make_printer, make_user, own


def test_lists_refillers_on_own_kiosks_only(db):
    mine = make_printer(db, printer_id="MINE")
    theirs = make_printer(db, printer_id="THEIRS")
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    own(db, owner, mine)

    rf = make_user(db, email="rf@test.in", is_refiller=True)
    db.add_all([
        PrinterRefiller(user_id=rf.id, printer_id=mine.id),
        PrinterRefiller(user_id=rf.id, printer_id=theirs.id),
    ])
    db.commit()

    out = kiosk_list_staff(db=db, current_user=owner)

    assert len(out) == 1
    assert out[0]["email"] == "rf@test.in"
    assert out[0]["printer_ids"] == [mine.id]


def test_assigns_a_refiller_to_an_owned_kiosk(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko2@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    rf = make_user(db, email="rf2@test.in", is_refiller=True)

    kiosk_assign_staff(printer_id=printer.id, user_id=rf.id, db=db, current_user=owner)

    assert db.query(PrinterRefiller).filter(
        PrinterRefiller.user_id == rf.id, PrinterRefiller.printer_id == printer.id
    ).count() == 1


def test_assigning_twice_does_not_duplicate(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko3@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    rf = make_user(db, email="rf3@test.in", is_refiller=True)

    kiosk_assign_staff(printer_id=printer.id, user_id=rf.id, db=db, current_user=owner)
    kiosk_assign_staff(printer_id=printer.id, user_id=rf.id, db=db, current_user=owner)

    assert db.query(PrinterRefiller).count() == 1


def test_cannot_assign_a_non_refiller(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko4@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    student = make_user(db, email="student@test.in")

    with pytest.raises(HTTPException) as exc:
        kiosk_assign_staff(
            printer_id=printer.id, user_id=student.id, db=db, current_user=owner
        )
    assert exc.value.status_code == 400


def test_cannot_assign_to_someone_elses_kiosk(db):
    printer = make_printer(db)
    stranger = make_user(db, email="ko5@test.in", is_kiosk_owner=True)
    rf = make_user(db, email="rf5@test.in", is_refiller=True)

    with pytest.raises(HTTPException) as exc:
        kiosk_assign_staff(
            printer_id=printer.id, user_id=rf.id, db=db, current_user=stranger
        )
    assert exc.value.status_code == 403


def test_unassign_removes_only_that_pairing(db):
    a = make_printer(db, printer_id="A")
    b = make_printer(db, printer_id="B")
    owner = make_user(db, email="ko6@test.in", is_kiosk_owner=True)
    own(db, owner, a)
    own(db, owner, b)
    rf = make_user(db, email="rf6@test.in", is_refiller=True)
    db.add_all([
        PrinterRefiller(user_id=rf.id, printer_id=a.id),
        PrinterRefiller(user_id=rf.id, printer_id=b.id),
    ])
    db.commit()

    kiosk_unassign_staff(printer_id=a.id, user_id=rf.id, db=db, current_user=owner)

    remaining = db.query(PrinterRefiller).all()
    assert [r.printer_id for r in remaining] == [b.id]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_kiosk_staff.py -q`
Expected: FAIL — `ImportError: cannot import name 'kiosk_list_staff'`

- [ ] **Step 3: Add the three endpoints**

In `app/routers/kiosk.py`:

```python
from app.models.printer_refiller import PrinterRefiller


@router.get("/kiosk/staff")
def kiosk_list_staff(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> list[dict[str, Any]]:
    """Refillers on this owner's kiosks, with which kiosks each one covers.

    Scoped to kiosks the caller owns, so a refiller who also works for another
    shop does not leak that shop's kiosk ids.
    """
    owned_ids = [
        row.printer_id
        for row in db.query(PrinterOwner).filter(PrinterOwner.user_id == current_user.id).all()
    ]
    if not owned_ids:
        return []

    rows = db.query(PrinterRefiller).filter(PrinterRefiller.printer_id.in_(owned_ids)).all()
    by_user: dict[int, list[int]] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row.printer_id)

    users = {u.id: u for u in db.query(User).filter(User.id.in_(list(by_user.keys()))).all()}
    return [
        {
            "user_id": uid,
            "email": users[uid].email,
            "printer_ids": sorted(printer_ids),
        }
        for uid, printer_ids in by_user.items()
        if uid in users
    ]


@router.post("/kiosk/printers/{printer_id}/staff/{user_id}")
def kiosk_assign_staff(
    printer_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> dict[str, Any]:
    """Put an existing refiller on one of your kiosks.

    Creating refiller logins stays admin-only — an owner can only assign an
    account the platform team already made.
    """
    printer = _assert_owns_printer(db, current_user, printer_id)

    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not target.is_refiller:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That account is not a refiller. Ask the platform team to create one.",
        )

    existing = (
        db.query(PrinterRefiller)
        .filter(
            PrinterRefiller.user_id == user_id,
            PrinterRefiller.printer_id == printer.id,
        )
        .first()
    )
    if existing is None:
        db.add(PrinterRefiller(user_id=user_id, printer_id=printer.id))
        record_audit(
            db,
            actor_id=current_user.id,
            action="staff.assign",
            entity_type="printer",
            entity_id=printer.id,
            after={"refiller_user_id": user_id},
        )
        db.commit()

    return {"status": "ok", "printer_id": printer.id, "user_id": user_id}


@router.delete("/kiosk/printers/{printer_id}/staff/{user_id}")
def kiosk_unassign_staff(
    printer_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> dict[str, Any]:
    """Take a refiller off one kiosk, leaving their other kiosks alone."""
    printer = _assert_owns_printer(db, current_user, printer_id)

    deleted = (
        db.query(PrinterRefiller)
        .filter(
            PrinterRefiller.user_id == user_id,
            PrinterRefiller.printer_id == printer.id,
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        record_audit(
            db,
            actor_id=current_user.id,
            action="staff.unassign",
            entity_type="printer",
            entity_id=printer.id,
            before={"refiller_user_id": user_id},
        )
    db.commit()
    return {
        "status": "ok",
        "printer_id": printer.id,
        "user_id": user_id,
        "removed": bool(deleted),
    }
```

- [ ] **Step 4: Run the tests and commit**

Run: `python -m pytest tests/test_kiosk_staff.py -q`
Expected: `6 passed`

Run: `python -m pytest -q`
Expected: `227 passed`

```bash
git add app/routers/kiosk.py tests/test_kiosk_staff.py
git commit -m "feat(staff): owners assign their own refillers to their own kiosks

Creating a refiller login stays admin-only; this is assignment of an existing
account, which previously only an admin could do."
```

---

### Task 11: Earnings

`/kiosk/revenue/by-day` and `/kiosk/summary` already exist. This adds the one
shape the owner Home screen needs and neither provides: a total for a period,
broken down per kiosk, with refunds excluded.

Note what this number means under the no-settlements rule: for an owner with
their own keys it is money already in **their** Razorpay account, not money
Printvendo owes them. The owner app must never present it as a balance due.

**Files:**
- Modify: `app/routers/kiosk.py`
- Test: `tests/test_kiosk_earnings.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_kiosk_earnings.py`:

```python
"""What did I make, and which kiosk made it.

Refunded payments count as zero, not as revenue — that is the whole reason a
refund flips Payment.status rather than deleting the row.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.payment import Payment
from app.routers.kiosk import kiosk_earnings
from tests.conftest import make_job, make_printer, make_user, own


def _payment(db, printer, student, amount, status="PAID", days_ago=0):
    job = make_job(db, student)
    p = Payment(
        job_id=job.id,
        user_id=student.id,
        printer_id=printer.id,
        amount=Decimal(amount),
        status=status,
    )
    p.created_at = datetime.utcnow() - timedelta(days=days_ago)
    db.add(p)
    db.commit()
    return p


def test_totals_per_kiosk_within_the_window(db):
    a = make_printer(db, printer_id="A", name="Library")
    b = make_printer(db, printer_id="B", name="Canteen")
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    own(db, owner, a)
    own(db, owner, b)
    student = make_user(db, email="s@test.in")

    _payment(db, a, student, "20.00", days_ago=1)
    _payment(db, a, student, "30.00", days_ago=2)
    _payment(db, b, student, "15.00", days_ago=1)

    out = kiosk_earnings(days=7, db=db, current_user=owner)

    assert out["total"] == 65.0
    per = {row["name"]: row["total"] for row in out["per_kiosk"]}
    assert per == {"Library": 50.0, "Canteen": 15.0}


def test_refunded_payments_do_not_count(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko2@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    student = make_user(db, email="s2@test.in")

    _payment(db, printer, student, "20.00")
    _payment(db, printer, student, "50.00", status="REFUNDED")

    out = kiosk_earnings(days=7, db=db, current_user=owner)
    assert out["total"] == 20.0


def test_payments_outside_the_window_are_excluded(db):
    printer = make_printer(db)
    owner = make_user(db, email="ko3@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    student = make_user(db, email="s3@test.in")

    _payment(db, printer, student, "20.00", days_ago=1)
    _payment(db, printer, student, "99.00", days_ago=40)

    out = kiosk_earnings(days=7, db=db, current_user=owner)
    assert out["total"] == 20.0


def test_owner_with_no_kiosks_gets_zero_not_an_error(db):
    owner = make_user(db, email="ko4@test.in", is_kiosk_owner=True)
    out = kiosk_earnings(days=7, db=db, current_user=owner)
    assert out["total"] == 0.0
    assert out["per_kiosk"] == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_kiosk_earnings.py -q`
Expected: FAIL — `ImportError: cannot import name 'kiosk_earnings'`

- [ ] **Step 3: Add the endpoint**

In `app/routers/kiosk.py`. Confirm `func`, `Query`, `datetime` and `timedelta`
are imported in that file and add whichever are missing:

```python
@router.get("/kiosk/earnings")
def kiosk_earnings(
    days: int = Query(default=7, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_kiosk_user),
) -> dict[str, Any]:
    """Takings over the last N days, in total and per kiosk.

    Only PAID counts. REFUNDED rows are deliberately left in the table and
    excluded here, so a refund reads as zero revenue rather than as a payment
    that never happened.
    """
    owned = (
        db.query(Printer)
        .join(PrinterOwner, PrinterOwner.printer_id == Printer.id)
        .filter(PrinterOwner.user_id == current_user.id)
        .all()
    )
    if not owned:
        return {"days": days, "total": 0.0, "per_kiosk": []}

    since = datetime.utcnow() - timedelta(days=days)
    by_printer = dict(
        db.query(Payment.printer_id, func.coalesce(func.sum(Payment.amount), 0))
        .filter(
            Payment.printer_id.in_([p.id for p in owned]),
            Payment.status == "PAID",
            Payment.created_at >= since,
        )
        .group_by(Payment.printer_id)
        .all()
    )

    per_kiosk = [
        {
            "printer_id": p.id,
            "public_id": p.printer_id,
            "name": p.name,
            "total": float(by_printer.get(p.id, 0) or 0),
        }
        for p in owned
    ]
    per_kiosk.sort(key=lambda row: row["total"], reverse=True)

    return {
        "days": days,
        "total": float(sum(row["total"] for row in per_kiosk)),
        "per_kiosk": per_kiosk,
    }
```

- [ ] **Step 4: Run the tests and commit**

Run: `python -m pytest tests/test_kiosk_earnings.py -q`
Expected: `4 passed`

Run: `python -m pytest -q`
Expected: `231 passed`

```bash
git add app/routers/kiosk.py tests/test_kiosk_earnings.py
git commit -m "feat(earnings): per-period, per-kiosk takings for the owner home screen"
```

---

# Part 3 — Admin endpoints

All of these hang off the existing `/owner/*` router, which is already guarded by
`get_current_admin_user`. Confirm that before starting:

```bash
grep -n "get_current_admin_user\|_get_owner_user" app/routers/owner.py | head -5
```

If individual routes carry the dependency rather than the router, add it to each
new route the same way the neighbouring routes do.

### Task 12: Plan CRUD

**Files:**
- Create: `app/schemas/plans.py`
- Modify: `app/routers/owner.py`
- Test: `tests/test_admin_plans.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_plans.py`:

```python
"""You set the plans, their prices, their kiosk caps and their price bands."""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.subscription_plan import PlanDiscount, SubscriptionPlan
from app.routers.owner import (
    admin_create_plan,
    admin_delete_plan,
    admin_list_plans,
    admin_set_plan_discounts,
    admin_update_plan,
)
from app.schemas.plans import DiscountsUpdate, PlanCreate, PlanUpdate
from tests.conftest import make_user


def _admin(db):
    return make_user(db, email="admin@test.in", is_admin=True)


def test_creates_a_plan(db):
    admin = _admin(db)
    out = admin_create_plan(
        payload=PlanCreate(
            name="Starter",
            monthly_price=Decimal("900.00"),
            max_kiosks=1,
            price_floor_bw=Decimal("1.00"),
            price_ceiling_bw=Decimal("8.00"),
        ),
        db=db,
        current_admin=admin,
    )
    assert out["name"] == "Starter"
    assert db.query(SubscriptionPlan).count() == 1


def test_lists_plans_with_their_discounts(db):
    admin = _admin(db)
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()
    db.add(PlanDiscount(plan_id=plan.id, duration_months=12, percent=Decimal("15")))
    db.commit()

    out = admin_list_plans(db=db, current_admin=admin)

    assert out[0]["discounts"] == [{"duration_months": 12, "percent": 15.0}]


def test_updates_only_supplied_fields(db):
    admin = _admin(db)
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()

    admin_update_plan(
        plan_id=plan.id,
        payload=PlanUpdate(monthly_price=Decimal("2000.00")),
        db=db,
        current_admin=admin,
    )

    db.refresh(plan)
    assert plan.monthly_price == Decimal("2000.00")
    assert plan.max_kiosks == 5  # untouched


def test_replaces_the_whole_discount_set(db):
    admin = _admin(db)
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()
    db.add(PlanDiscount(plan_id=plan.id, duration_months=6, percent=Decimal("10")))
    db.commit()

    admin_set_plan_discounts(
        plan_id=plan.id,
        payload=DiscountsUpdate(discounts=[{"duration_months": 12, "percent": Decimal("20")}]),
        db=db,
        current_admin=admin,
    )

    rows = db.query(PlanDiscount).filter(PlanDiscount.plan_id == plan.id).all()
    assert [(r.duration_months, float(r.percent)) for r in rows] == [(12, 20.0)]


def test_deleting_deactivates_rather_than_removing(db):
    """Subscriptions point at plans; deleting the row would orphan them."""
    admin = _admin(db)
    plan = SubscriptionPlan(name="Old", monthly_price=Decimal("500.00"), max_kiosks=1)
    db.add(plan)
    db.commit()

    admin_delete_plan(plan_id=plan.id, db=db, current_admin=admin)

    db.refresh(plan)
    assert plan.is_active is False
    assert db.query(SubscriptionPlan).count() == 1


def test_unknown_plan_is_404(db):
    admin = _admin(db)
    with pytest.raises(HTTPException) as exc:
        admin_update_plan(
            plan_id=999, payload=PlanUpdate(max_kiosks=2), db=db, current_admin=admin
        )
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_admin_plans.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.plans'`

- [ ] **Step 3: Write the schemas**

Create `app/schemas/plans.py`:

```python
from decimal import Decimal

from pydantic import BaseModel, Field

PLAN_FIELDS = (
    "name",
    "monthly_price",
    "max_kiosks",
    "price_floor_bw",
    "price_ceiling_bw",
    "price_floor_color",
    "price_ceiling_color",
    "is_active",
)


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    monthly_price: Decimal = Field(ge=0, le=1_000_000)
    max_kiosks: int = Field(ge=1, le=10_000)
    price_floor_bw: Decimal | None = Field(default=None, ge=0, le=1000)
    price_ceiling_bw: Decimal | None = Field(default=None, ge=0, le=1000)
    price_floor_color: Decimal | None = Field(default=None, ge=0, le=1000)
    price_ceiling_color: Decimal | None = Field(default=None, ge=0, le=1000)


class PlanUpdate(BaseModel):
    """Every field optional — omitted means unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    monthly_price: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    max_kiosks: int | None = Field(default=None, ge=1, le=10_000)
    price_floor_bw: Decimal | None = Field(default=None, ge=0, le=1000)
    price_ceiling_bw: Decimal | None = Field(default=None, ge=0, le=1000)
    price_floor_color: Decimal | None = Field(default=None, ge=0, le=1000)
    price_ceiling_color: Decimal | None = Field(default=None, ge=0, le=1000)
    is_active: bool | None = None

    def supplied(self) -> dict:
        return {
            f: getattr(self, f) for f in PLAN_FIELDS if getattr(self, f, None) is not None
        }


class DiscountItem(BaseModel):
    duration_months: int = Field(ge=1, le=60)
    percent: Decimal = Field(ge=0, le=100)


class DiscountsUpdate(BaseModel):
    """The complete discount set for a plan. Replaces whatever is there.

    Replace rather than merge: a discount you deleted in the console must
    actually disappear, and a merge API cannot express deletion.
    """

    discounts: list[DiscountItem]
```

- [ ] **Step 4: Add the endpoints**

In `app/routers/owner.py`:

```python
from app.models.subscription_plan import PlanDiscount, SubscriptionPlan
from app.schemas.plans import DiscountsUpdate, PlanCreate, PlanUpdate
from app.services.audit import record_audit


def _plan_or_404(db: Session, plan_id: int) -> SubscriptionPlan:
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return plan


def _plan_dict(db: Session, plan: SubscriptionPlan) -> dict:
    discounts = (
        db.query(PlanDiscount)
        .filter(PlanDiscount.plan_id == plan.id)
        .order_by(PlanDiscount.duration_months)
        .all()
    )
    return {
        "id": plan.id,
        "name": plan.name,
        "monthly_price": float(plan.monthly_price),
        "max_kiosks": plan.max_kiosks,
        "price_floor_bw": float(plan.price_floor_bw) if plan.price_floor_bw is not None else None,
        "price_ceiling_bw": float(plan.price_ceiling_bw) if plan.price_ceiling_bw is not None else None,
        "price_floor_color": float(plan.price_floor_color) if plan.price_floor_color is not None else None,
        "price_ceiling_color": float(plan.price_ceiling_color) if plan.price_ceiling_color is not None else None,
        "is_active": plan.is_active,
        "discounts": [
            {"duration_months": d.duration_months, "percent": float(d.percent)} for d in discounts
        ],
    }


@router.get("/owner/plans")
def admin_list_plans(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> list[dict]:
    """Every plan, active or not, with its discount ladder."""
    plans = db.query(SubscriptionPlan).order_by(SubscriptionPlan.id).all()
    return [_plan_dict(db, p) for p in plans]


@router.post("/owner/plans", status_code=status.HTTP_201_CREATED)
def admin_create_plan(
    payload: PlanCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    plan = SubscriptionPlan(**payload.model_dump())
    db.add(plan)
    db.flush()
    record_audit(
        db,
        actor_id=current_admin.id,
        action="plan.create",
        entity_type="plan",
        entity_id=plan.id,
        after=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(plan)
    return _plan_dict(db, plan)


@router.put("/owner/plans/{plan_id}")
def admin_update_plan(
    plan_id: int,
    payload: PlanUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Change a plan. Existing subscriptions keep the price they were sold at.

    Subscription rows store their own monthly_price and total_amount, so
    editing a plan changes what the NEXT subscription costs, never an
    existing bill.
    """
    plan = _plan_or_404(db, plan_id)
    changes = payload.supplied()
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Send at least one field to change"
        )

    before = {f: getattr(plan, f) for f in changes}
    for field, value in changes.items():
        setattr(plan, field, value)

    record_audit(
        db,
        actor_id=current_admin.id,
        action="plan.update",
        entity_type="plan",
        entity_id=plan.id,
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in changes.items()},
    )
    db.commit()
    db.refresh(plan)
    return _plan_dict(db, plan)


@router.put("/owner/plans/{plan_id}/discounts")
def admin_set_plan_discounts(
    plan_id: int,
    payload: DiscountsUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Replace this plan's whole discount ladder."""
    plan = _plan_or_404(db, plan_id)

    before = [
        {"duration_months": d.duration_months, "percent": float(d.percent)}
        for d in db.query(PlanDiscount).filter(PlanDiscount.plan_id == plan.id).all()
    ]
    db.query(PlanDiscount).filter(PlanDiscount.plan_id == plan.id).delete(
        synchronize_session=False
    )
    for item in payload.discounts:
        db.add(
            PlanDiscount(
                plan_id=plan.id,
                duration_months=item.duration_months,
                percent=item.percent,
            )
        )

    record_audit(
        db,
        actor_id=current_admin.id,
        action="plan.discounts",
        entity_type="plan",
        entity_id=plan.id,
        before={"discounts": before},
        after={"discounts": payload.model_dump(mode="json")["discounts"]},
    )
    db.commit()
    return _plan_dict(db, plan)


@router.delete("/owner/plans/{plan_id}")
def admin_delete_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Retire a plan by deactivating it.

    Never deletes the row: live subscriptions reference it, and their history
    has to stay readable long after the plan stops being sold.
    """
    plan = _plan_or_404(db, plan_id)
    plan.is_active = False
    record_audit(
        db,
        actor_id=current_admin.id,
        action="plan.deactivate",
        entity_type="plan",
        entity_id=plan.id,
        before={"is_active": True},
        after={"is_active": False},
    )
    db.commit()
    return {"status": "ok", "plan_id": plan.id, "is_active": False}
```

- [ ] **Step 5: Run the tests and commit**

Run: `python -m pytest tests/test_admin_plans.py -q`
Expected: `6 passed`

Run: `python -m pytest -q`
Expected: `237 passed`

```bash
git add app/schemas/plans.py app/routers/owner.py tests/test_admin_plans.py
git commit -m "feat(admin): plan and discount management"
```

---

### Task 13: Negotiated terms per owner

You negotiate a price and a free period with each owner. This is where that
lands.

**Files:**
- Modify: `app/routers/owner.py`
- Test: `tests/test_admin_terms.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_terms.py`:

```python
"""Per-owner commercial terms: a negotiated price and a free period."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.subscription import Subscription
from app.models.subscription_plan import SubscriptionPlan
from app.routers.owner import admin_set_subscription_terms
from app.schemas.plans import TermsUpdate
from tests.conftest import make_user


def _sub(db, owner, plan):
    sub = Subscription(
        user_id=owner.id,
        plan_tier=plan.name,
        plan_id=plan.id,
        monthly_price=plan.monthly_price,
        settlement_type="DIRECT",
        duration_months=1,
        total_amount=plan.monthly_price,
        status="ACTIVE",
    )
    db.add(sub)
    db.commit()
    return sub


def test_sets_a_negotiated_price(db):
    admin = make_user(db, email="admin@test.in", is_admin=True)
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()
    sub = _sub(db, owner, plan)

    admin_set_subscription_terms(
        user_id=owner.id,
        payload=TermsUpdate(negotiated_price=Decimal("1200.00")),
        db=db,
        current_admin=admin,
    )

    db.refresh(sub)
    assert sub.negotiated_price == Decimal("1200.00")


def test_sets_a_free_period(db):
    admin = make_user(db, email="admin2@test.in", is_admin=True)
    owner = make_user(db, email="ko2@test.in", is_kiosk_owner=True)
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()
    sub = _sub(db, owner, plan)

    until = datetime.utcnow() + timedelta(days=180)
    admin_set_subscription_terms(
        user_id=owner.id, payload=TermsUpdate(free_until=until), db=db, current_admin=admin
    )

    db.refresh(sub)
    assert sub.free_until is not None


def test_clearing_a_term_is_possible(db):
    """Setting negotiated_price back to the plan price must be expressible."""
    admin = make_user(db, email="admin3@test.in", is_admin=True)
    owner = make_user(db, email="ko3@test.in", is_kiosk_owner=True)
    plan = SubscriptionPlan(name="Pro", monthly_price=Decimal("1800.00"), max_kiosks=5)
    db.add(plan)
    db.commit()
    sub = _sub(db, owner, plan)
    sub.negotiated_price = Decimal("1200.00")
    db.commit()

    admin_set_subscription_terms(
        user_id=owner.id,
        payload=TermsUpdate(clear_negotiated_price=True),
        db=db,
        current_admin=admin,
    )

    db.refresh(sub)
    assert sub.negotiated_price is None


def test_owner_without_a_subscription_is_404(db):
    admin = make_user(db, email="admin4@test.in", is_admin=True)
    owner = make_user(db, email="ko4@test.in", is_kiosk_owner=True)

    with pytest.raises(HTTPException) as exc:
        admin_set_subscription_terms(
            user_id=owner.id,
            payload=TermsUpdate(negotiated_price=Decimal("100.00")),
            db=db,
            current_admin=admin,
        )
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_admin_terms.py -q`
Expected: FAIL — `ImportError: cannot import name 'TermsUpdate'`

- [ ] **Step 3: Add the schema**

Append to `app/schemas/plans.py`:

```python
from datetime import datetime


class TermsUpdate(BaseModel):
    """Per-owner overrides of the plan.

    The explicit clear_* flags exist because None already means "leave this
    alone" — without them there is no way to express "put it back to the plan
    price".
    """

    plan_id: int | None = None
    negotiated_price: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    free_until: datetime | None = None
    clear_negotiated_price: bool = False
    clear_free_until: bool = False
```

- [ ] **Step 4: Add the endpoint**

In `app/routers/owner.py`:

```python
@router.put("/owner/subscriptions/{user_id}/terms")
def admin_set_subscription_terms(
    user_id: int,
    payload: "TermsUpdate",
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Set the negotiated price and free period for one owner.

    Applies to their current subscription, which is the only one being
    charged. Past subscriptions keep the terms they were sold under.
    """
    sub = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id)
        .order_by(Subscription.id.desc())
        .first()
    )
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That owner has no subscription to set terms on",
        )

    before = {
        "plan_id": sub.plan_id,
        "negotiated_price": str(sub.negotiated_price) if sub.negotiated_price is not None else None,
        "free_until": sub.free_until.isoformat() if sub.free_until else None,
    }

    if payload.plan_id is not None:
        sub.plan_id = payload.plan_id
    if payload.clear_negotiated_price:
        sub.negotiated_price = None
    elif payload.negotiated_price is not None:
        sub.negotiated_price = payload.negotiated_price
    if payload.clear_free_until:
        sub.free_until = None
    elif payload.free_until is not None:
        sub.free_until = payload.free_until

    after = {
        "plan_id": sub.plan_id,
        "negotiated_price": str(sub.negotiated_price) if sub.negotiated_price is not None else None,
        "free_until": sub.free_until.isoformat() if sub.free_until else None,
    }

    record_audit(
        db,
        actor_id=current_admin.id,
        action="subscription.terms",
        entity_type="subscription",
        entity_id=sub.id,
        before=before,
        after=after,
    )
    db.commit()
    return {"status": "ok", "subscription_id": sub.id, **after}
```

Add `from app.schemas.plans import TermsUpdate` to the imports and drop the
quotes around the annotation.

- [ ] **Step 5: Run the tests and commit**

Run: `python -m pytest tests/test_admin_terms.py -q`
Expected: `4 passed`

```bash
git add app/schemas/plans.py app/routers/owner.py tests/test_admin_terms.py
git commit -m "feat(admin): negotiated price and free period per owner"
```

---

### Task 14: Onboarding stage, with the keys gate

`onboarding_stage` exists (Task 1 of the previous plan) but nothing moves it.
This adds the control **and the rule that makes the no-settlements model safe**:
an owned kiosk cannot go LIVE until its owner's Razorpay keys are configured.
Without that gate, a shop's kiosk would collect student money into the platform
account with no mechanism to ever forward it.

**Files:**
- Modify: `app/routers/owner.py`
- Test: `tests/test_onboarding.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding.py`:

```python
"""A kiosk goes live only when it can actually pay its owner.

There are no settlements. If an owned kiosk went LIVE before its owner
configured Razorpay keys, every rupee it collected would land in the platform
account with no way to reach the shop.
"""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.kiosk_payment_config import KioskPaymentConfig
from app.models.subscription import Subscription
from app.routers.owner import admin_get_onboarding, admin_set_onboarding
from app.schemas.plans import OnboardingUpdate
from tests.conftest import make_printer, make_user, own


def _admin(db):
    return make_user(db, email="admin@test.in", is_admin=True)


def _owner_with_keys(db, printer, email="ko@test.in"):
    owner = make_user(db, email=email, is_kiosk_owner=True)
    own(db, owner, printer)
    owner.subscription_enabled = True
    db.add(Subscription(
        user_id=owner.id, plan_tier="Pro", monthly_price=Decimal("1800.00"),
        settlement_type="DIRECT", duration_months=1,
        total_amount=Decimal("1800.00"), status="ACTIVE",
    ))
    db.add(KioskPaymentConfig(
        user_id=owner.id, razorpay_key_id="k", razorpay_key_secret="s", is_configured=True
    ))
    db.commit()
    return owner


def test_reads_the_current_stage(db):
    admin = _admin(db)
    printer = make_printer(db)

    out = admin_get_onboarding(printer_id=printer.id, db=db, current_admin=admin)

    assert out["onboarding_stage"] == "REGISTERED"
    assert out["kiosk_type"] == "PLATFORM"


def test_moves_a_kiosk_through_a_stage(db):
    admin = _admin(db)
    printer = make_printer(db)

    admin_set_onboarding(
        printer_id=printer.id,
        payload=OnboardingUpdate(onboarding_stage="APPROVED", onboarding_note="paperwork in"),
        db=db,
        current_admin=admin,
    )

    db.refresh(printer)
    assert printer.onboarding_stage == "APPROVED"
    assert printer.onboarding_note == "paperwork in"


def test_owned_kiosk_cannot_go_live_without_keys(db):
    admin = _admin(db)
    printer = make_printer(db)
    owner = make_user(db, email="nokeys@test.in", is_kiosk_owner=True)
    own(db, owner, printer)

    with pytest.raises(HTTPException) as exc:
        admin_set_onboarding(
            printer_id=printer.id,
            payload=OnboardingUpdate(onboarding_stage="LIVE"),
            db=db,
            current_admin=admin,
        )
    assert exc.value.status_code == 400
    assert "razorpay" in str(exc.value.detail).lower()

    db.refresh(printer)
    assert printer.onboarding_stage == "REGISTERED"


def test_owned_kiosk_with_keys_can_go_live(db):
    admin = _admin(db)
    printer = make_printer(db)
    _owner_with_keys(db, printer)

    admin_set_onboarding(
        printer_id=printer.id,
        payload=OnboardingUpdate(onboarding_stage="LIVE"),
        db=db,
        current_admin=admin,
    )

    db.refresh(printer)
    assert printer.onboarding_stage == "LIVE"


def test_platform_kiosk_goes_live_without_keys(db):
    """We are the owner, so our own keys already take the money."""
    admin = _admin(db)
    printer = make_printer(db)

    admin_set_onboarding(
        printer_id=printer.id,
        payload=OnboardingUpdate(onboarding_stage="LIVE"),
        db=db,
        current_admin=admin,
    )

    db.refresh(printer)
    assert printer.onboarding_stage == "LIVE"


def test_rejects_an_unknown_stage(db):
    admin = _admin(db)
    printer = make_printer(db)

    with pytest.raises(Exception):
        admin_set_onboarding(
            printer_id=printer.id,
            payload=OnboardingUpdate(onboarding_stage="BANANA"),
            db=db,
            current_admin=admin,
        )


def test_sets_kiosk_type(db):
    admin = _admin(db)
    printer = make_printer(db)

    admin_set_onboarding(
        printer_id=printer.id,
        payload=OnboardingUpdate(kiosk_type="SAAS"),
        db=db,
        current_admin=admin,
    )

    db.refresh(printer)
    assert printer.kiosk_type == "SAAS"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_onboarding.py -q`
Expected: FAIL — `ImportError: cannot import name 'OnboardingUpdate'`

- [ ] **Step 3: Add the schema**

Append to `app/schemas/plans.py`:

```python
from typing import Literal

KioskType = Literal["PLATFORM", "SOLD", "SAAS"]
OnboardingStage = Literal["REGISTERED", "APPROVED", "PRICED", "KEYS", "KYC", "LIVE"]


class OnboardingUpdate(BaseModel):
    """Move a kiosk along, or correct what kind of kiosk it is.

    kiosk_type is backfilled by inference and is expected to be wrong until a
    human confirms it here.
    """

    kiosk_type: KioskType | None = None
    onboarding_stage: OnboardingStage | None = None
    onboarding_note: str | None = Field(default=None, max_length=500)
```

- [ ] **Step 4: Add the endpoints**

In `app/routers/owner.py`:

```python
from app.schemas.plans import OnboardingUpdate
from app.services.gateway_routing import owner_of, resolves_to_owner_gateway


@router.get("/owner/kiosks/{printer_id}/onboarding")
def admin_get_onboarding(
    printer_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Where this kiosk is in getting live, and what is blocking it."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if printer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Printer not found")

    owner = owner_of(db, printer)
    return {
        "printer_id": printer.id,
        "kiosk_type": printer.kiosk_type,
        "onboarding_stage": printer.onboarding_stage,
        "onboarding_note": printer.onboarding_note,
        "owner_email": owner.email if owner else None,
        "keys_configured": resolves_to_owner_gateway(db, printer),
        "accepts_wallet": bool(printer.accepts_wallet),
    }


@router.put("/owner/kiosks/{printer_id}/onboarding")
def admin_set_onboarding(
    printer_id: int,
    payload: OnboardingUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Move a kiosk's onboarding stage, or correct its type.

    Going LIVE on an OWNED kiosk requires the owner's Razorpay keys to be
    configured and their subscription active, so student money reaches them
    directly. There are no settlements — an owner's takings never pass through
    the platform — so a kiosk must not start selling before the route to its
    owner exists. The gate protects the owner.
    """
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if printer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Printer not found")

    if payload.onboarding_stage == "LIVE":
        owner = owner_of(db, printer)
        if owner is not None and not resolves_to_owner_gateway(db, printer):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This kiosk's owner has not finished connecting their Razorpay "
                    "account, so their takings would arrive in Printvendo's account "
                    "instead of theirs. Finish the Razorpay step, then set it live."
                ),
            )

    before = {
        "kiosk_type": printer.kiosk_type,
        "onboarding_stage": printer.onboarding_stage,
        "onboarding_note": printer.onboarding_note,
    }
    if payload.kiosk_type is not None:
        printer.kiosk_type = payload.kiosk_type
    if payload.onboarding_stage is not None:
        printer.onboarding_stage = payload.onboarding_stage
    if payload.onboarding_note is not None:
        printer.onboarding_note = payload.onboarding_note

    after = {
        "kiosk_type": printer.kiosk_type,
        "onboarding_stage": printer.onboarding_stage,
        "onboarding_note": printer.onboarding_note,
    }
    record_audit(
        db,
        actor_id=current_admin.id,
        action="kiosk.onboarding",
        entity_type="printer",
        entity_id=printer.id,
        before=before,
        after=after,
    )
    db.commit()
    return {"status": "ok", "printer_id": printer.id, **after}
```

- [ ] **Step 5: Run the tests and commit**

Run: `python -m pytest tests/test_onboarding.py -q`
Expected: `7 passed`

Run: `python -m pytest -q`
Expected: `248 passed`

```bash
git add app/schemas/plans.py app/routers/owner.py tests/test_onboarding.py
git commit -m "feat(admin): onboarding stages, gated on the owner being payable

An owned kiosk cannot go LIVE until its owner's Razorpay keys are configured
and their subscription is active. There are no settlements, so a kiosk that
collects into the platform account can never pay its owner."
```

---

### Task 15: The work queue

Six scattered admin pages collapse into one list with filters. Every row is
something that needs you to do something.

**Files:**
- Modify: `app/routers/owner.py`
- Test: `tests/test_admin_queue.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_queue.py`:

```python
"""One list of everything waiting on you."""
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.kiosk_payment_config import KioskPaymentConfig
from app.models.subscription import Subscription
from app.routers.owner import admin_queue
from tests.conftest import make_printer, make_user, own


def _admin(db):
    return make_user(db, email="admin@test.in", is_admin=True)


def test_unapproved_kiosks_appear(db):
    admin = _admin(db)
    make_printer(db, printer_id="NEW", is_approved=False)

    out = admin_queue(db=db, current_admin=admin)

    kinds = {row["kind"] for row in out["items"]}
    assert "kiosk_approval" in kinds


def test_stalled_onboarding_appears(db):
    admin = _admin(db)
    printer = make_printer(db, printer_id="STUCK")
    printer.onboarding_stage = "KEYS"
    owner = make_user(db, email="ko@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    db.commit()

    out = admin_queue(db=db, current_admin=admin)

    stalled = [r for r in out["items"] if r["kind"] == "onboarding_stalled"]
    assert stalled and stalled[0]["entity_id"] == printer.id


def test_expiring_subscriptions_appear(db):
    admin = _admin(db)
    owner = make_user(db, email="ko2@test.in", is_kiosk_owner=True)
    db.add(Subscription(
        user_id=owner.id, plan_tier="Pro", monthly_price=Decimal("1800.00"),
        settlement_type="DIRECT", duration_months=1,
        total_amount=Decimal("1800.00"), status="ACTIVE",
        expires_at=datetime.utcnow() + timedelta(days=5),
    ))
    db.commit()

    out = admin_queue(db=db, current_admin=admin)

    assert any(r["kind"] == "subscription_expiring" for r in out["items"])


def test_a_live_healthy_kiosk_is_not_in_the_queue(db):
    admin = _admin(db)
    printer = make_printer(db, printer_id="FINE", is_approved=True)
    printer.onboarding_stage = "LIVE"
    owner = make_user(db, email="ko3@test.in", is_kiosk_owner=True)
    own(db, owner, printer)
    owner.subscription_enabled = True
    db.add(Subscription(
        user_id=owner.id, plan_tier="Pro", monthly_price=Decimal("1800.00"),
        settlement_type="DIRECT", duration_months=1,
        total_amount=Decimal("1800.00"), status="ACTIVE",
        expires_at=datetime.utcnow() + timedelta(days=200),
    ))
    db.add(KioskPaymentConfig(
        user_id=owner.id, razorpay_key_id="k", razorpay_key_secret="s", is_configured=True
    ))
    db.commit()

    out = admin_queue(db=db, current_admin=admin)

    assert not [r for r in out["items"] if r["entity_id"] == printer.id]


def test_filtering_by_kind(db):
    admin = _admin(db)
    make_printer(db, printer_id="NEW", is_approved=False)

    out = admin_queue(kind="subscription_expiring", db=db, current_admin=admin)

    assert out["items"] == []
```

Check `make_printer`'s real signature first — if it has no `is_approved`
argument, set the attribute after creating the printer instead.

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_admin_queue.py -q`
Expected: FAIL — `ImportError: cannot import name 'admin_queue'`

- [ ] **Step 3: Add the endpoint**

In `app/routers/owner.py`:

```python
EXPIRY_WARNING_DAYS = 14


@router.get("/owner/queue")
def admin_queue(
    kind: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Everything waiting on the platform owner, as one list.

    Replaces six separate pages. Each row carries the entity it refers to so
    the console can route straight to it, and `kind` filters rather than
    navigates — the point is that this is one list, not six.
    """
    items: list[dict] = []

    # Kiosks awaiting approval.
    for p in db.query(Printer).filter(Printer.is_approved == False).all():  # noqa: E712
        items.append({
            "kind": "kiosk_approval",
            "entity_type": "printer",
            "entity_id": p.id,
            "title": f"{p.name} is waiting for approval",
            "detail": p.printer_id,
        })

    # Owned kiosks that started onboarding and never finished.
    stalled = (
        db.query(Printer)
        .filter(Printer.onboarding_stage.notin_(["LIVE", "REGISTERED"]))
        .all()
    )
    for p in stalled:
        items.append({
            "kind": "onboarding_stalled",
            "entity_type": "printer",
            "entity_id": p.id,
            "title": f"{p.name} is stuck at {p.onboarding_stage}",
            "detail": p.onboarding_note,
        })

    # Subscriptions expiring soon.
    horizon = datetime.utcnow() + timedelta(days=EXPIRY_WARNING_DAYS)
    expiring = (
        db.query(Subscription)
        .filter(
            Subscription.status == "ACTIVE",
            Subscription.expires_at.isnot(None),
            Subscription.expires_at <= horizon,
        )
        .all()
    )
    for s in expiring:
        items.append({
            "kind": "subscription_expiring",
            "entity_type": "subscription",
            "entity_id": s.id,
            "title": "Subscription expiring",
            "detail": s.expires_at.isoformat() if s.expires_at else None,
        })

    if kind:
        items = [row for row in items if row["kind"] == kind]

    return {"count": len(items), "items": items}
```

Confirm `Query`, `datetime`, `timedelta`, `Printer` and `Subscription` are
imported in `owner.py`; add whichever are missing.

- [ ] **Step 4: Run the tests and commit**

Run: `python -m pytest tests/test_admin_queue.py -q`
Expected: `5 passed`

```bash
git add app/routers/owner.py tests/test_admin_queue.py
git commit -m "feat(admin): one work queue replacing six scattered pages"
```

---

### Task 16: Audit feed and search

**Files:**
- Modify: `app/routers/owner.py`
- Test: `tests/test_admin_audit_feed.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_audit_feed.py`:

```python
"""Reading the audit trail, and finding things by name."""
from app.routers.owner import admin_audit, admin_search
from app.services.audit import record_audit
from tests.conftest import make_printer, make_user


def _admin(db):
    return make_user(db, email="admin@test.in", is_admin=True)


def test_audit_returns_newest_first(db):
    admin = _admin(db)
    for i in range(3):
        record_audit(
            db, actor_id=admin.id, action=f"a{i}", entity_type="printer", entity_id=i
        )
    db.commit()

    out = admin_audit(db=db, current_admin=admin)

    assert [row["action"] for row in out["items"]] == ["a2", "a1", "a0"]


def test_audit_filters_by_entity(db):
    admin = _admin(db)
    record_audit(db, actor_id=admin.id, action="x", entity_type="printer", entity_id=1)
    record_audit(db, actor_id=admin.id, action="y", entity_type="plan", entity_id=1)
    db.commit()

    out = admin_audit(entity_type="plan", db=db, current_admin=admin)

    assert [row["action"] for row in out["items"]] == ["y"]


def test_search_finds_a_kiosk_by_name(db):
    admin = _admin(db)
    make_printer(db, printer_id="LIB1", name="Library Block A")

    out = admin_search(q="library", db=db, current_admin=admin)

    assert [r["type"] for r in out["results"]] == ["kiosk"]
    assert out["results"][0]["label"] == "Library Block A"


def test_search_finds_an_owner_by_email(db):
    admin = _admin(db)
    make_user(db, email="shopkeeper@test.in", is_kiosk_owner=True)

    out = admin_search(q="shopkeeper", db=db, current_admin=admin)

    assert any(r["type"] == "owner" for r in out["results"])


def test_search_needs_two_characters(db):
    admin = _admin(db)
    make_printer(db, printer_id="L", name="Library")

    out = admin_search(q="l", db=db, current_admin=admin)

    assert out["results"] == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_admin_audit_feed.py -q`
Expected: FAIL — `ImportError: cannot import name 'admin_audit'`

- [ ] **Step 3: Add both endpoints**

In `app/routers/owner.py`:

```python
from app.models.admin_audit_log import AdminAuditLog

SEARCH_MIN_CHARS = 2
SEARCH_LIMIT = 20


@router.get("/owner/audit")
def admin_audit(
    entity_type: str | None = Query(default=None),
    entity_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """The audit trail, newest first, optionally scoped to one entity."""
    query = db.query(AdminAuditLog)
    if entity_type:
        query = query.filter(AdminAuditLog.entity_type == entity_type)
    if entity_id is not None:
        query = query.filter(AdminAuditLog.entity_id == entity_id)

    rows = query.order_by(AdminAuditLog.id.desc()).limit(limit).all()
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "actor_id": r.actor_id,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "before": r.before,
                "after": r.after,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/owner/search")
def admin_search(
    q: str = Query(min_length=0),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> dict:
    """Find a kiosk or an owner by name, public id or email.

    Deliberately short: the console routes from a result to the full page for
    that entity, so this returns identity and nothing else.
    """
    term = (q or "").strip()
    if len(term) < SEARCH_MIN_CHARS:
        return {"query": term, "results": []}

    like = f"%{term}%"
    results: list[dict] = []

    kiosks = (
        db.query(Printer)
        .filter(or_(Printer.name.ilike(like), Printer.printer_id.ilike(like)))
        .limit(SEARCH_LIMIT)
        .all()
    )
    results.extend(
        {
            "type": "kiosk",
            "id": p.id,
            "label": p.name,
            "detail": p.printer_id,
        }
        for p in kiosks
    )

    owners = (
        db.query(User)
        .filter(User.is_kiosk_owner == True, User.email.ilike(like))  # noqa: E712
        .limit(SEARCH_LIMIT)
        .all()
    )
    results.extend(
        {"type": "owner", "id": u.id, "label": u.email, "detail": None} for u in owners
    )

    return {"query": term, "results": results}
```

Confirm `or_` is imported from `sqlalchemy` in that file; add it if not.

- [ ] **Step 4: Run the tests and commit**

Run: `python -m pytest tests/test_admin_audit_feed.py -q`
Expected: `5 passed`

Run: `python -m pytest -q`
Expected: `258 passed`

```bash
git add app/routers/owner.py tests/test_admin_audit_feed.py
git commit -m "feat(admin): audit feed and global search"
```

---

### Task 17: Mark settlements as retired

Owners are paid directly, so there is nothing to settle. The model and
endpoints stay until the old dashboard is switched off, but nothing new may
build on them and the next person to read the file must not think they are
live.

**Files:**
- Modify: `app/routers/owner.py`, `app/routers/kiosk.py`, `app/models/settlement.py`
- Test: `tests/test_no_settlements.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_no_settlements.py`:

```python
"""Settlements are retired. Nothing new may depend on them.

Owners are paid directly through their own Razorpay keys — student money never
passes through Printvendo, so there is nothing to settle. The settlement code
survives only to keep the old dashboard running until it is switched off.
"""
import inspect

from app.routers import kiosk, owner

NEW_ENDPOINTS = [
    "kiosk_get_pricing",
    "kiosk_set_pricing",
    "kiosk_refund_job",
    "kiosk_list_staff",
    "kiosk_earnings",
]


def test_no_new_owner_endpoint_mentions_settlement():
    for name in NEW_ENDPOINTS:
        source = inspect.getsource(getattr(kiosk, name))
        assert "settlement" not in source.lower(), f"{name} must not use settlements"


def test_settlement_module_says_it_is_retired():
    import app.models.settlement as settlement_module

    doc = (settlement_module.__doc__ or "").lower()
    assert "retired" in doc, "settlement.py must document that settlements are retired"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_no_settlements.py -q`
Expected: FAIL — the settlement module has no such docstring.

- [ ] **Step 3: Document the retirement**

At the very top of `app/models/settlement.py`, above the imports:

```python
"""RETIRED — there is nothing to settle.

Kiosk owners are paid directly: student money goes from the student to the
owner's own Razorpay account without passing through Printvendo. Settlement
existed for the old model, where the platform collected on a keyless owner's
behalf. That model is gone — configured keys are now required before a kiosk
goes live. This model and the /owner/settlements/* and /kiosk/settlements
endpoints survive only to keep the old admin dashboard running until it is
switched off.

Do not build on this. New code that needs "what did this owner earn" wants
kiosk_earnings in routers/kiosk.py, which reports money already in the
owner's own account.
"""
```

Add a one-line pointer above the settlement routes in `app/routers/owner.py`
and `app/routers/kiosk.py`:

```python
# RETIRED — see app/models/settlement.py. No new code may call these.
```

- [ ] **Step 4: Run the tests and commit**

Run: `python -m pytest tests/test_no_settlements.py -q`
Expected: `2 passed`

Run: `python -m pytest -q`
Expected: `260 passed`

```bash
git add app/models/settlement.py app/routers/owner.py app/routers/kiosk.py tests/test_no_settlements.py
git commit -m "docs(settlements): mark the settlement path retired

Owners are paid directly, so there is nothing to settle. The model and
endpoints survive only until the old dashboard is switched off; the test stops
new owner-facing endpoints from growing a dependency on them."
```

---

### Task 18: Update the backend CLAUDE.md

**Files:**
- Modify: `cloud-backend/CLAUDE.md`

- [ ] **Step 1: Record what changed**

Add to the "Conventions & gotchas" list:

```markdown
- **Owners are paid directly; there are no settlements.** Student money goes
  straight to the owner's own Razorpay account and never passes through
  Printvendo, which is why configured keys are a precondition of going live.
  `app/models/settlement.py` and the `/owner/settlements/*`
  endpoints are retired and exist only for the old dashboard. Consequences:
  wallet balance is spendable only at platform kiosks
  (`app/services/wallet_eligibility.py`), an owned kiosk cannot reach
  `onboarding_stage = LIVE` until its owner's Razorpay keys are configured, and
  a gateway payment held in an owner's account can only be refunded to source.
- **One answer for gateway routing.** `app/services/gateway_routing.py`
  decides whose Razorpay collects at a kiosk. Wallet eligibility, refunds and
  payment creation all defer to it. Never re-derive that rule inline.
- **Plans live in the database.** `subscription_plans` and `plan_discounts`,
  seeded by `seed_subscription_plans.py` to the numbers that used to be
  hardcoded in `routers/subscription.py`. Price floors and ceilings on the plan
  bound what an owner may charge students.
- Plan tables and audit log: `migrate_add_plan_tables.py`, then
  `seed_subscription_plans.py`. Run before swapping the container.
```

Update the test count on the last line of that file from 169 to the number the
suite actually reports.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the no-settlements rule and the new services"
```

---

## Rollout

Run in this order on the VPS. Migrations and the seed run before the container
swap, so no request ever sees a half-built schema:

```bash
cd /opt/printit/cloud-backend
git fetch origin && git checkout vps-migration-hardening
git reset --hard origin/vps-migration-hardening
cd deploy
docker compose build api
docker compose run --rm api python migrate_add_plan_tables.py
docker compose run --rm api python seed_subscription_plans.py
docker compose up -d api
docker compose logs -f api
```

The seed must run before the swap: `_calculate_total` falls back to the legacy
dicts when the table is empty, so an unseeded database keeps charging correctly,
but the console would show no plans to edit.

## Testing

Beyond the per-task tests, the suite must continue to prove:

- wallet holds rejected at any kiosk resolving to an owner gateway
- prices outside the plan band rejected, and nothing written on rejection
- refunds to wallet refused for money held in a shop's account
- an owned kiosk without configured keys cannot reach LIVE
- plan maths producing the same totals as the hardcoded values

## Risks

**Editing a plan's price does not change existing bills.** Subscription rows
carry their own `monthly_price` and `total_amount`. This is deliberate, and the
console must say so where you edit a price, or you will expect a change that
never comes.

**The LIVE gate can lock you out of your own kiosks** if `kiosk_type` inference
was wrong and a platform kiosk was given an owner. Check `admin_get_onboarding`'s
`keys_configured` before assuming the gate is broken.

**`admin_queue` scans three tables unpaginated.** Fine at the current scale;
revisit when any one of them passes a few thousand rows.
