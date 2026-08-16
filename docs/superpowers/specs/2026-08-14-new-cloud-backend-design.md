# New Cloud Backend — Design

**Date:** 2026-08-14
**Status:** Approved design, pending implementation plan
**Replaces:** `cloud-backend/` (which stays deployed and untouched until cutover)

---

## 1. Why

The current backend works and earns money. It is also structurally set up to keep
producing the same class of bug.

Its routers are organised by **audience**, not by **subject**:

| Router | Lines | What it is |
|---|---|---|
| `kiosk.py` | 2059 | everything a kiosk owner can do |
| `owner.py` | 1883 | everything a platform admin can do — over the same subjects |
| `payments.py` | 1294 | |
| `jobs.py` | 1150 | |
| **total** | **14167** | 16 routers, ~150 routes, 25 models |

Because the split is by audience, **the same subject gets a separate endpoint
per audience**, each re-deriving its own authorisation:

- paper reset is exposed 3 times (`kiosk.py`, `admin.py`, `refiller.py`)
- clear-queue twice, pricing update twice
  (`/printers/{id}/pricing` and `/kiosk/printers/{id}/pricing`)
- the Razorpay webhook handler exists 3 times, so 3 URLs must be registered

**Accuracy note, checked 2026-08-14:** the *logic* behind paper reset and
clear-queue is no longer duplicated — `services/printer_ops.py` was extracted
and all three routers call it. An earlier version of this document repeated the
2026-07-12 audit's claim of four copies; that was stale. What remains duplicated
is the route surface and the per-router authorisation, which is the part that
actually bites.

Copies of a *rule* drift, and that is not hypothetical here: the wallet money
leak was caused by "whose Razorpay collects at this kiosk" being re-derived in
two places that disagreed. The lesson is that shared logic is not enough on its
own — the authorisation decision has to be shared too, which is what §6's single
scope resolver is for.

Everything below follows from fixing that root cause.

### Non-goals

- Changing what the product does. Operational behaviour is preserved exactly.
- Touching production. `cloud-backend/` stays deployed and serving until cutover.
- Changing any UI. Frontend work is confined to each app's `lib/` API layer.

---

## 2. Decisions taken

| # | Decision | Rationale |
|---|---|---|
| D1 | Full redesign in a new directory + new DB. Production stays running. | Deploy later, after parallel verification. |
| D2 | API contract redesigned. Frontend changes confined to `lib/`. | 4 files in `printvendo-web`, 1 in `printvendo-owner`. No page/component/design changes. |
| D3 | FastAPI + SQLAlchemy 2.0 + Postgres, done properly. | pi-agent is Python, Razorpay/pypdf/Ghostscript tooling is Python, existing 305 tests are portable as reference. Lowest risk. |
| D4 | Clients served: `printvendo-web`, `printvendo-owner`, `pi-agent`, new admin console, `printit-refiller-app`. | Old `printit-admin-dashboard` and old end-user PWA are **not** served. |
| D5 | Carried over: photo printing, guest login, web push, admin alerts. | All verified live in code, not dead. |
| D6 | Modular monolith organised by bounded context. | The only option that structurally prevents the duplicate-implementation drift. |
| D7 | SOLD/SAAS kiosk with lapsed subscription or missing keys **stops accepting payments**. | Money never lands in the wrong account. Gives owners a hard reason to pay. |
| D8 | Wallet unchanged: PLATFORM-kiosk-only, no platform fee, platform liability. | Zero behaviour change; the rule becomes explicit instead of emergent. |
| D9 | `min(2% of base, ₹2)` fee charged on **every** gateway payment. At owner-gateway kiosks the owner keeps it. | Deliberate commercial decision — see §7. Do not "fix" this later. |
| D10 | Refiller staffing: owner invites by email, refiller accepts. | Closes the cross-tenant leak in §6. |
| D11 | Device contract redesigned. New agent written fresh and deployed **over SSH, kiosk by kiosk**. | Operator has SSH to every kiosk, so the in-band auto-update mechanism is not on the critical path. See §10. |
| D12 | Settlements deleted. | Retired by existing docs; owners are paid directly. Historical rows archived to CSV. |
| D13 | Admin can grant a **trial** to any owner (SOLD or SAAS) and can override **both** the monthly price and the duration discounts **per owner**. | See §5a. |

### D13 in detail — commercial terms are per owner, not only per plan

Requested 2026-08-14. Three of the four pieces already exist in the backend
being replaced and carry over unchanged:

| Lever | Where it lives today | Carries over |
|---|---|---|
| Trial | `Subscription.free_until` — treats the subscription as active and unbilled until that date | yes |
| Per-owner monthly price | `Subscription.negotiated_price` | yes |
| Duration discounts (6/12 month) | `PlanDiscount(plan_id, duration_months, percent)` | yes, as the **default ladder** |
| **Per-owner duration discount** | **does not exist** | **new** |

The new piece is `OwnerDiscount(user_id, duration_months, percent)`, which
overrides the plan's ladder for one owner. Resolution order when pricing a
subscription, most specific first:

1. `OwnerDiscount` for that owner and duration
2. `PlanDiscount` for that plan and duration
3. no discount

`negotiated_price` replaces the plan's monthly price before any discount is
applied, so an owner can hold both a bespoke rate and a bespoke annual discount.

**A trial is a money-routing lever, not just a billing courtesy.** `free_until`
makes a subscription count as ACTIVE, and an active subscription plus configured
keys is exactly what `kiosk_payment_gate` requires to return `OWNER_GATEWAY`. So
granting a trial to a SAAS owner lets their kiosk go LIVE and collect into their
own account. That is the intended behaviour — it is how a trial is useful — but
it means expiry of a trial demotes the kiosk to `SUSPENDED_BILLING` exactly like
a lapsed paid subscription, via `kiosks.onboarding.reconcile_billing_state`.

Trials apply to both SOLD and SAAS. PLATFORM kiosks have no owner subscription
and are unaffected.

---

## 2a. Every legacy defect, and what makes it impossible

From `2026-08-15-legacy-data-audit.md`. Each row names a real problem found in
production data and the **mechanism** — not the intention — that prevents it
recurring. A rule with no mechanism is a comment, and comments do not hold.

| Legacy defect | Mechanism in the new backend | Status |
|---|---|---|
| Ten case-duplicate accounts (`Person@` and `person@`), each with its own wallet | Unique index on `lower(email)`. An application check cannot hold this alone — two concurrent registrations both pass it and both insert. | **done**, mutation-tested |
| `Payment.status` documented as 3 values, actually 6; `CAPTURED` and `PAID` both mean success | Status is an enum; "did this succeed" is one predicate that no caller re-expresses as a string comparison | pending, sub-project 5 |
| `wallet.py`/`printers.py` filter `PAID` only; `kiosk.py` filters `PAID`+`CAPTURED` | Same predicate, one definition, used by every consumer | pending, sub-project 5 |
| Wallet `balance` disagrees with its own ledger; ₹500 entered one balance with no ledger row | **The ledger is the record.** Balance is derived from it, never written independently. Migration rebuilds every balance and reports each delta. | pending, sub-project 5 |
| A `SOLD` kiosk with no owner — its takings route to the platform | `kiosk_payment_gate` returns `CLOSED` when an owner-gateway kiosk has no owner who can collect, and a `CLOSED` kiosk cannot be listed or ordered from | **done** (gate stub fails closed), real gate in 5 |
| Every kiosk stuck at `onboarding_stage = REGISTERED`; the LIVE gate was never in the path | Stage is an enum with a validated transition table, and the gate is the only route to `LIVE` | **done**, mutation-tested |
| Paid payments with no print job (the two-call wallet hazard) | `Order` aggregate: payment and print tasks commit in one transaction, so the state is unreachable | pending, sub-project 5 |
| `REFUND_PENDING` written by one line, read by nothing, accumulating real money owed | Refunds are a state with an owner and a worklist surfaced in the admin console | pending, sub-projects 5 and 6 |
| Owner Razorpay secrets in plaintext while a comment claimed otherwise | Fernet ciphertext; no plaintext column exists to write to; no endpoint returns it | **done**, mutation-tested |
| Test and dev kiosks live in production; probable duplicate kiosk | Migration quarantines rather than deletes, with a list for human confirmation | pending, sub-project 8 |
| Thousands of abandoned checkouts and payment-free guests, never cleaned | Quarantined at migration; not imported | pending, sub-project 8 |
| Refresh tokens never pruned | Not migrated at all — everyone signs in again | decided |

**The pattern behind all of them.** Every defect above is a rule that lived in
one place and was not enforced in another: a check in Python but not in the
database, a status set in one router and read differently in the next, a gate
built but never put in the path. The new backend's answer is not "be more
careful" — it is that each rule has exactly one implementation, and something
mechanical fails when it is bypassed.

## 3. Architecture

```
app/
  core/          config, db, security, errors, money, ids
  modules/
    identity/    users, roles, sessions, google/guest login
    kiosks/      registry, kiosk_type, onboarding, paper, pricing, staff
    printing/    documents, files, pipeline, queue, print tasks, device hub
    payments/    razorpay, orders, refunds, gateway routing, fees
    wallet/      balance, ledger, topups
    billing/     plans, subscriptions, invoices
    ops/         admin alerts, audit log, analytics, exports
  api/
    student/     printvendo-web
    owner/       printvendo-owner
    admin/       new admin console
    refiller/    printit-refiller-app
    device/      pi-agent (+ WebSocket)
```

**Rules, enforced by `import-linter` in CI:**

- A module owns its tables. No other module may import its ORM models.
- Modules communicate only through each other's typed service functions.
- `api/` handlers may not import ORM models or unscoped repositories at all.
  They authenticate, validate, call one service, serialise.

Five audiences, one implementation of paper. The four-copies problem is deleted
by construction rather than by discipline.

**Rejected alternatives:** classic layered (layers are not boundaries — the god
file re-forms one level down, which is how the current code got here); separate
services (a payment and a print job must commit together; distributed
transactions and three deploys are wrong at this scale — revisit past ~100
kiosks).

**Runtime:** single process, single Postgres, plus **Redis** for the device
WebSocket registry. The current `--workers 1` constraint exists only because
`ws_manager` holds sockets in a per-process dict; moving that to Redis pub/sub
removes it.

---

## 4. Domain model

The core defect: `Job` (uploaded doc), `PrinterJob` (dispatch) and `Payment`
(money) each independently reference user and printer, and **nothing owns the
transaction**. Consequences:

- `POST /wallet/hold` marks a `Payment` PAID but creates no `PrinterJob`. The
  caller must remember a second `POST /printers/{id}/print` or the student paid
  for a job that never prints. (Documented as a live hazard in
  `printvendo-web/CLAUDE.md`.)
- `Payment.job_id` is NOT NULL, so one payment cannot span three files —
  hence a whole second endpoint, `/wallet/hold/multi`.

**Fix: `Order` is the aggregate.** One student, one kiosk, N documents, one
payment, N print tasks — created in a single transaction.

| Today | New | Why |
|---|---|---|
| — | **`Order`** | The missing aggregate. Pay and enqueue commit together or not at all. |
| `Job` | **`Document`** | An uploaded file + options + page count. Never was a "job". |
| `PrinterJob` | **`PrintTask`** | One document dispatched to one device; owns queue state. |
| `Payment` | `Payment` | Now belongs to `Order`, not to a single document. |
| `Printer` (30 cols) | **`Kiosk`** + **`KioskDevice`** + **`KioskPaper`** | One table was doing business unit + device identity + pricing + paper + email-nag state. Split by what changes it and who may change it. |
| `printer_owners` + `printer_refillers` | **`KioskAssignment(kiosk, user, role)`** | Two structurally identical tables. |
| `is_admin`/`is_kiosk_owner`/`is_refiller`/`is_guest`/`subscription_enabled` | **`UserRole(user, role)`** | Loose booleans are how a refiller nearly reaches money data. |
| `Settlement`, `Subscription.settlement_type` | *deleted* | Retired. `settlement_type` is NOT NULL and meaningless. |
| `Notification` | **`AdminAlert`** | It is admin-scoped by design (`is_admin_only`, created in `printers.py:78`). Named for what it is. |

**Identity rule.** Today `printer_id` is *both* a public string and a numeric
primary key, used inconsistently — `printvendo-web/CLAUDE.md` carries a written
warning about passing the wrong one. New rule: **every id crossing the API is an
opaque prefixed string** (`ksk_7f3a`, `ord_91bc`, `doc_44de`). Numeric primary
keys never leave the database. The bug class is unreachable.

**Money naming.** `Job.price_cents` is a `Numeric(10,2)` holding **rupees**. The
name is a lie and becomes `amount_inr`. Rupees throughout, `Decimal` never
`float`.

---

## 5. Workflows

### The one gate

Three services independently re-derive whose Razorpay collects at a kiosk today:
`gateway_routing`, `wallet_eligibility`, `refunds`. Two of them disagreeing
caused the wallet leak. Replaced by exactly one function:

```
kiosk_payment_gate(kiosk) -> OWNER_GATEWAY | PLATFORM_GATEWAY | CLOSED
```

Every consumer calls it — order pricing, wallet eligibility, refund destination,
kiosk listing. Nothing re-derives it.

**D7 lives here.** A SOLD/SAAS kiosk with no active subscription, or no
configured keys, returns `CLOSED`. A `CLOSED` kiosk is not listed and cannot
accept an order.

### Lifecycles

```
Document   UPLOADED → PROCESSING → READY
                          ↓            ↓
                      REJECTED      EXPIRED (file purged by retention)

Order      DRAFT → AWAITING_PAYMENT → PAID → DISPATCHED → COMPLETED
                          ↓             ↓                     ↓
                       EXPIRED      REFUNDED           PARTIALLY_FAILED

PrintTask  QUEUED → SENT_TO_DEVICE → PRINTING → PRINTED
                          ↓              ↓          ↓
                       BLOCKED        FAILED    (retention purge)

Kiosk      REGISTERED → APPROVED → CONFIGURED → LIVE ⇄ MAINTENANCE
                                                  ↓
                                         SUSPENDED_BILLING → LIVE
                                                  ↓
                                               RETIRED
```

- `CONFIGURED` is auto-skipped for PLATFORM kiosks (platform keys already exist).
- `SUSPENDED_BILLING` is entered automatically when the gate returns `CLOSED`.
  The lapse rule is visible as state, not as an emergent side effect.
- `MAINTENANCE` stays owner-settable. `ERROR` and `DISABLED` stay admin-only.

### Student print flow

```
pick kiosk → upload documents (server prices them against THAT kiosk)
  → POST /orders
  → server: reserve paper, compute price, open Razorpay order if needed
  → pay (wallet OR gateway)
  → ONE transaction: Payment=PAID, Order=PAID, PrintTasks created, WS push
```

Wallet and gateway are two branches into the *same* commit. There is no path
where money moves and print tasks do not exist.

Server price is authoritative. Client-side estimates (`lib/price.ts`) stay
estimates and are never charged.

### Other flows

- **Wallet top-up:** order → webhook → ledger credit, idempotent on
  `razorpay_payment_id`.
- **Refund:** destination forced by the gate. Owner-collected money can only go
  back to source; platform-collected may go to wallet or source. The illegal
  combination is refused server-side.
- **Paper refill:** owner or assigned refiller sets sheets (never a percentage —
  both tray size and amount are editable), clears nag state, kiosk leaves
  out-of-paper.
- **Subscription:** plan → order → webhook → ACTIVE. `free_until` implements the
  "N free months" term for SOLD kiosks. Expiry → grace → gate `CLOSED`.
- **Device:** register → token → WebSocket connect → heartbeat → receive tasks.
- **Kiosk onboarding by type:** PLATFORM installs skip `CONFIGURED`; SOLD and
  SAAS require owner keys + active subscription before `LIVE`.

### Business model (authoritative)

| Kiosk type | Who installs | Whose Razorpay | Printvendo earns |
|---|---|---|---|
| `PLATFORM` | us | ours | the print revenue |
| `SOLD` | they buy the hardware from us | theirs | subscription, after N free months (`free_until`) |
| `SAAS` | their printer, our software | theirs | subscription |

---

## 6. Security & authorisation

**Invariant: an owner controls only their own kiosks. Only admin controls all
kiosks.** This is already the intent, but it is currently enforced by remembering
to call a helper. Two places where that already failed:

### Finding 1 — owner Razorpay secrets are stored in plaintext

`KioskPaymentConfig.razorpay_key_secret` is a plain `String` column. Its comment
claims *"stored encrypted/hashed"*, but `grep -rn "encrypt|Fernet|cryptography"
app/` returns **only that comment**. There is no encryption anywhere in the
backend. A Postgres dump exposes every kiosk owner's live payment-gateway
credentials — which matters immediately, because the migration starts with
`pg_dump`.

### Finding 2 — staff assignment is cross-tenant

`POST /kiosk/printers/{printer_id}/staff/{user_id}` (`kiosk.py:911`) verifies
that the caller owns the kiosk and that the target has `is_refiller`, but never
that the refiller has any relationship to the caller. So an owner can:

1. **Enumerate the user table by response code** — 404 (no such user), 400 (user
   exists, not a refiller), 200 (user exists and is a refiller).
2. **Bind another shop's refiller to their own kiosk**, after which
   `GET /kiosk/staff` returns that person's **email and full name**.

Owner A harvests owner B's staff identity. Same class as the IDOR the existing
`owner.py:44` warning is about.

### The model

Every kiosk-scoped query passes through one resolver:

```
scope = kiosk_scope(actor)     # OWNER    → their assignments
                               # REFILLER → their assignments
                               # ADMIN    → all
repo.kiosks(scope)             # no unscoped query exists in api/ code
```

Admin is **not a bypass path with its own unchecked router** (today's `/owner/*`).
Admin is the same resolver with a wider scope. The "DO NOT LOOSEN" hazard
disappears because there is no second path to loosen.

### Controls

| Area | Rule |
|---|---|
| Secrets at rest | Owner Razorpay secrets encrypted (envelope encryption, key from env/KMS). Never returned by any API — masked to `rzp_live_••••4821`. |
| Tokens | Access JWT 15 min; rotating refresh with **reuse detection** — a replayed refresh revokes the whole family. Refresh tokens hashed at rest (as today). |
| Device auth | Per-device token, hashed at rest, scoped to one kiosk. Rotation endpoint; revoked on unassign. |
| File access | `documents/{id}/file` is owner-only. The Pi receives a short-lived signed URL bound to its `PrintTask`, not a device-token-wide path. |
| PDF pipeline | Ghostscript runs `-dSAFER`, no shell, hard timeout, page/size caps, scratch dir it cannot escape. |
| Webhooks | One endpoint. Signature verified. Replay blocked by the unique constraint on `razorpay_payment_id`. |
| Student PII | Owner/refiller responses use a serialiser with no name/email/user_id field. Cannot leak what the type does not contain. CSV export keeps `_csv_safe` formula-injection neutralisation. |
| Audit | Every owner/refiller/admin mutation records actor, scope, before/after. |
| Rate limits | Auth, upload, order creation, payment verify. Real client IP via proxy headers. |
| Enumeration | Identical response whether or not an account exists. |
| Staffing (D10) | Owner enters an email. Existing account → invite; no account → admin asked to mint one. **Identical response either way.** No binding, and no name/detail disclosure, until the refiller accepts. |

---

## 7. API contract

Five audiences, five prefixes, honest names. `/owner` finally means owner.

```
/v1/app/*        printvendo-web
/v1/owner/*      printvendo-owner
/v1/admin/*      new admin console
/v1/refiller/*   printit-refiller-app
/v1/device/*     pi-agent  (+ /v1/device/ws)
```

### Student

```
POST /v1/app/documents               upload → {id, pages, amount_inr}
POST /v1/app/orders                  {kiosk_id, document_ids[]} → {id, total, fee, payable}
POST /v1/app/orders/{id}/pay         {method: wallet|gateway}
POST /v1/app/orders/{id}/pay/verify  (gateway only)
```

`pay` with `method=wallet` is **one call that commits everything** — debit,
order PAID, print tasks created, WebSocket push. The `/wallet/hold` +
`/printers/{id}/print` two-step and the separate `/wallet/hold/multi` are both
gone.

Also: `/auth/*` (register, login, google, guest, refresh, logout, me); `/kiosks`,
`/kiosks/{id}`, favourites; `/documents/analyze`; `/documents/photo-layout`;
`/orders` history, `/orders/{id}/invoice(.pdf)`; `/wallet`, `/wallet/ledger`,
`/wallet/topup`; `/shop/items`; `/push/*`.

Paper-shop purchases stop being a fake print job — they are an `Order` with a
shop line item.

### Owner

`/me`; `/kiosks`, `/kiosks/{id}`; `/kiosks/{id}/pricing|paper|status|queue|device`;
`/kiosks/{id}/orders` (+ force-complete, refund); `/earnings`; `/analytics`;
`/orders/export`; `/subscriptions` (+ invoice); `/payment-config`;
`/bank-details`; `/staff`; `/refill-logs`.

The price band comes from the server alongside the prices — no second copy of the
plan's floor/ceiling in the client to drift.

### Refiller

`/me`; `/kiosks`; `/kiosks/{id}/paper`; `/kiosks/{id}/refill-logs`. Paper only,
no money — enforced by role against the shared service, not by a separate copy of
the code.

### Admin

Kiosk approval/assignment/onboarding; accounts and role minting; plans and
discounts; subscription overrides (`negotiated_price`, `free_until`); payment-config
request review (with an **authenticated proof-image proxy** — the current admin
dashboard builds `API_BASE + '/storage/...'` which 404s silently, so proofs are
approved unseen); alerts; audit; global search; revenue.

### Contract rules

- Errors keep `{"detail": "<human sentence>"}`. `printvendo-owner/lib/api.ts`
  surfaces `detail` verbatim by design and those sentences are written for
  humans. Not replaced with codes.
- `Idempotency-Key` required on every money-moving POST.
- CORS allowlist from env, not hardcoded in `main.py`.
- One Razorpay webhook endpoint, not three. One URL to register.
- Static export constraint: query params, not dynamic segments, in client routes
  (`/kiosk?id=N`) — unchanged, but the contract must not assume path routing.

**~150 routes → ~85.** Not from removing features — from paper existing once
instead of four times, refunds once instead of twice, pricing once instead of
twice.

### Money rules (D8, D9)

- Wallet debits the **base price only**, no fee, and is spendable **only at
  PLATFORM kiosks** — top-ups land in the platform's Razorpay, so spending at an
  owner-gateway kiosk would mean the platform keeps the cash while the owner
  prints for free.
- Gateway payments add `min(2% of base, ₹2)`.
- **At owner-gateway kiosks that fee is collected by the owner, not the
  platform.** This is intentional (D9). The platform's revenue from SOLD/SAAS
  kiosks is the subscription. Do not "correct" this.
- `lib/fees.ts` in `printvendo-web` mirrors the formula; the totals must agree
  with what the checkout sheet opens at.

---

## 8. Migration from production

`razorpay_order_id` is the key that makes the missing `Order` recoverable:
`/wallet/hold/multi` writes **one** ledger row and N `Payment` rows sharing
`razorpay_order_id = "WALLET:{ledger_id}"` (`wallet.py:598-632`); gateway
payments share a real Razorpay order id.

> **Every set of legacy payments sharing a `razorpay_order_id` becomes exactly
> one `Order`.** Payments with no shared id become single-document orders. No
> guessing.

### Mechanics

`pg_dump` production → restore to a local read-only `printit_legacy` database.
Migration code lives in `migration/` in the new repo and reads legacy through
**raw SQL only** — legacy ORM models never enter the new codebase, or the old
shapes return through the side door.

Every new table keeps a nullable indexed `legacy_id`. The run is therefore
**idempotent and re-runnable**: a second pass upserts rather than duplicates.
`legacy_id` is kept permanently for traceability.

### Table map

| Legacy | New | Note |
|---|---|---|
| `users` | `users` + `user_roles` | booleans → role rows |
| `printers` | `kiosks` + `kiosk_devices` + `kiosk_paper` | one → three |
| `printer_owners` + `printer_refillers` | `kiosk_assignments` | two → one |
| `jobs` | `documents` | `price_cents` → `amount_inr` (already rupees) |
| `payments` | `orders` + `payments` | grouped by `razorpay_order_id` |
| `printer_jobs` | `print_tasks` | |
| `wallets`, `wallet_ledger` | same | balances checksummed |
| `subscriptions`, `subscription_plans`, `plan_discounts` | same | `settlement_type` dropped |
| `kiosk_payment_configs` | same, **encrypted** | plaintext secrets encrypted on load |
| `payment_config_requests` | same | |
| `paper_shop_items`, `paper_refill_logs` | same | |
| `user_favorite_printers` | `favorites` | |
| `bank_details` | same | |
| `notifications` | `admin_alerts` | |
| `admin_audit_log` | `audit_log` | |
| `push_subscriptions` | same | VAPID keys carried over |
| `settlements` | **not migrated** | exported to CSV and archived — retired, but financial history |
| `refresh_tokens` | **not migrated** | everyone re-logs-in; a free security reset |

### Cleaning rules

Nothing is deleted silently. Anything dropped goes to a `quarantine` table with a
reason. The run prints a reconciliation report — row counts in/out/quarantined
per table, plus wallet-balance and lifetime-revenue totals that **must match
production exactly or the run fails**.

- Orphans (document with missing user, task with missing kiosk) → quarantine
- Guest accounts with zero orders → quarantine
- Documents whose files retention already purged → row kept, marked `EXPIRED`
- Dangling or duplicate Razorpay ids → quarantine
- **Kiosks whose `kiosk_type` is still the `PLATFORM` default but that have an
  owner** → flagged for manual review before load. Not auto-decided: getting it
  wrong routes money to the wrong Razorpay account.

### Files

`storage/original|converted|templates` copied by path, verified by count and byte
size against the database rows that reference them.

---

## 9. Testing

Two suites carry the weight:

1. **Authorisation matrix — generated, not handwritten.** Every route × every
   role × (own scope / other scope / no scope) → expected status, generated from
   the route table. **A new route with no matrix entry fails the build.** This is
   what makes §6's invariant non-regressable.
2. **Money invariants as property tests.** Wallet balance never negative;
   `sum(ledger) == balance` per user; `order.total == sum(document amounts) + fee`;
   refunds never exceed captured; every payment resolves to exactly one gateway.
   Run against generated histories, not hand-picked examples.

Plus: per-audience contract fixtures; migration reconciliation against a real
production snapshot; Razorpay test mode with webhook replay; device and
WebSocket integration tests; the retention sweep.

TDD per `superpowers:test-driven-development`.

---

## 10. Deploy & cutover

**Stack:** new repo/directory, side by side with production. Docker Compose —
Postgres + Redis + API + Caddy. Alembic migrations run as a pre-start job (not 27
ad-hoc `migrate_*.py` scripts). Staging domain (e.g. `api2.innvera.online`) so the
new stack is fully live and testable while production keeps serving.

**Agent rollout is operator-driven over SSH (D11).** The operator has SSH access to
every kiosk, so the new agent is deployed directly rather than through the in-band
`trigger-update` mechanism. That removes the "bricked remote Pi" failure mode from
the critical path: a failed kiosk is recovered over the same SSH session.

The new agent is written fresh against `/v1/device/*` and carries a
**`BACKEND_URL` + `PROTOCOL` config**, so a kiosk can be pointed at staging for
verification and at production at cutover without a redeploy.

Rollout safety:
- previous agent kept on disk at `agent.prev/`; a one-line SSH command reverts
- each kiosk verified against staging (register → heartbeat → test print) before
  cutover night
- new agent binary is **staged on every kiosk in advance**, inert, pointed at
  staging. Cutover is then a config flip plus a service restart, not a deploy.

### The freeze window is unavoidable

Students and kiosks must move together. A kiosk switched to the new backend while
students are still ordering on the old one produces two live ledgers; and the API
hostname cannot simply be repointed, because any agent not yet migrated would hit
the new backend speaking the old protocol.

Therefore: **one coordinated window**, during which no order can be taken. Its
length is (delta migration) + (SSH restart of every kiosk), so it scales with
fleet size. Run it at the lowest-traffic hour.

**No DNS flip at cutover.** The new backend lives at its own hostname from day
one. Clients are repointed to that hostname; `innvera.online` stays on the old
stack until the new one has been stable long enough to retire it, and only then
is the domain moved.

**Cutover sequence:**

1. Snapshot production, migrate, verify reconciliation report
2. Point dev builds of `printvendo-web` and `printvendo-owner` at the new
   hostname; exercise every flow
3. Stage the new agent on every kiosk over SSH, pointed at staging. Verify a real
   end-to-end print on a canary kiosk.
4. **Window opens** — production set read-only; no new orders
5. Incremental delta migration (upsert by `legacy_id`); reconciliation must pass
6. Deploy frontends with the new API hostname; SSH-restart every kiosk's agent
   onto the new protocol
7. Smoke test: one real order, one real print, one real refund
8. **Window closes.** Old stack stays up, intact and rollback-able, until retired.

Rollback at any point before step 8 is: revert agent config over SSH, redeploy
previous frontend build, un-freeze the old backend. The old database was never
written to by the new stack.

---

## 11. Frontend work required

Confined to API layers. **No page, component, or design changes.**

| App | Files |
|---|---|
| `printvendo-web` | `lib/api.ts`, `lib/flowApi.ts`, `lib/queries.ts`, `lib/batches.ts` (+ `lib/pushNotifications.ts` endpoints) |
| `printvendo-owner` | `lib/api.ts` |
| `printit-refiller-app` | 5 inline `fetch` call sites in `index.html`, `dashboard.html` |
| `pi-agent` | **rewritten** against `/v1/device/*`; deployed over SSH (see §10) |
| admin console | built fresh against `/v1/admin/*` |

`printvendo-web`'s `lib/jobState.ts` display mapping needs updating for the new
`Order`/`PrintTask` enums — the rule that backend enums are never rendered
directly still holds.

---

## 12. Decomposition

This spec is the system design. Implementation is decomposed into sequenced
sub-projects, each getting its own plan:

1. **Foundation** — repo, core (config, db, ids, money, errors, security), Alembic, CI with `import-linter`, auth matrix harness
2. **identity** — users, roles, sessions, login methods
3. **kiosks** — registry, types, onboarding, pricing, paper, assignments, staff invites
4. **printing** — documents, pipeline, orders' print side, tasks, device hub + Redis
5. **payments + wallet + billing** — the gate, orders, Razorpay, refunds, ledger, subscriptions
6. **ops** — alerts, audit, analytics, exports
7. **api layers** — student, owner, refiller, admin, device
8. **migration** — extract, transform, reconcile
9. **agent rollout + cutover**

Order matters: 5 depends on 3 and 4; 8 depends on all of 2–6.

---

## Appendix — carried-over gotchas

Verified against the current code; these must survive the rewrite.

- Money is rupees, `Decimal`, never `float`.
- Server price is authoritative; client estimates are display-only.
- Owner-facing responses carry no student identity.
- `_csv_safe` formula-injection neutralisation on exports (filenames are
  attacker-controlled).
- Wallet debit is a single atomic conditional `UPDATE`, not read-check-write —
  this is what closed the concurrent double-spend race.
- `razorpay_payment_id` is nullable-unique — this is what blocks replayed webhook
  credits.
- Retention keys on task state, not document status.
- Plan price floors/ceilings bound what an owner may charge, enforced server-side
  on every pricing write.
- Paper is sheets, never a percentage; tray size and fill amount both editable.
- Ghostscript must be present for the PDF pipeline.
