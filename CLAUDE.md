# printvendo-backend

The rebuilt central API. Replaces `cloud-backend/`, which stays deployed and
serving production until cutover. See
`docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md` for the design
and the reasoning behind everything below.

## Stack

Python 3.12, FastAPI, SQLAlchemy 2.0, Postgres, Alembic. Redis is a declared
dependency but nothing connects to it until the device WebSocket hub lands.

## Commands

```bash
py -3.12 -m venv .venv               # NOT `python` — that is 3.13 on this machine
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m uvicorn app.main:create_app --factory --reload --port 8000
.venv/Scripts/python -m pytest -q
.venv/Scripts/lint-imports           # module boundary contracts
.venv/Scripts/python -m ruff check .
```

Copy `.env.example` to `.env` and fill it. The app refuses to boot with a
`JWT_SECRET_KEY` under 32 characters, and in production without a
`RAZORPAY_WEBHOOK_SECRET` or with a wildcard CORS origin.

**Database.** The machine's local Postgres 18 service on port 5432, role
`printvendo`, databases `printvendo` and `printvendo_test`. Tests require
`printvendo_test` to exist and fail loudly rather than silently skip.

## Layout

- `app/core/` — primitives every module needs: `config`, `db`, `ids`, `money`,
  `errors`, `crypto`, `security`. Depends on nothing else in `app`.
- `app/modules/` — bounded contexts. A module owns its tables; no other module
  may import its ORM models, only its service functions.
- `app/api/` — thin per-audience route layers (`student`, `owner`, `admin`,
  `refiller`, `device`). Authenticate, validate, call one service, serialise.
- `migrations/` — Alembic. URL comes from `DATABASE_URL`, never `alembic.ini`.

## Conventions that are not preferences

- **Organise by subject, never by audience.** The old backend's routers were
  per-audience, so paper reset existed four times and clear-queue twice, and the
  copies drifted. Five audiences share one implementation of each subject.
- **`import-linter` enforces the above** (`.importlinter`, run in CI and by
  `tests/test_architecture.py`). Verified to exit 1 on a violation, so a
  boundary breach fails the build rather than becoming precedent.
- **Every route needs an entry in `tests/authz/matrix.py`** or the build fails.
  Adding a route forces you to state who may call it.
- **Money is `Decimal` rupees**, two places, `ROUND_HALF_UP`.
  `app.core.money.as_money` raises on `float` rather than accepting a value that
  has already lost precision. The old backend had a `price_cents` column holding
  rupees — the name was a lie.
- **Public ids are opaque and prefixed** (`ksk_…`, `ord_…`). Numeric primary keys
  never leave the database, so a caller cannot pass the wrong kind of id.
- **Errors are `{"detail": "<human sentence>"}`.** `printvendo-owner` renders
  `detail` straight to the user; those strings are product copy, not debug
  output. Unexpected exceptions are logged and replaced with a generic sentence.
- **Tests run on real Postgres, never SQLite.** The old backend's SQLite-in-dev
  split let dialect bugs through.
- **Third-party secrets are encrypted at rest** via `app.core.crypto.SecretBox`
  and returned only masked. The old backend stored owner Razorpay key secrets in
  plaintext while claiming in a comment that it did not.
- **`app/main.py` has no module-level `app` instance.** Building one at import
  time would make importing the module require full config, breaking pytest
  collection, Alembic and import-linter. Serve with
  `uvicorn app.main:create_app --factory`.
- **`migrations/script.py.mako` is modernised** so generated revisions are
  ruff-clean. Do not restore Alembic's stock template.
- **Workers may exceed 1.** The device WebSocket registry will live in Redis, not
  a per-process dict — the constraint the old backend could never lift.
- **Refresh rotation has a 60-second grace window** (`identity/sessions.py`).
  Removing it reintroduces the old backend's "logs out frequently" bug, where
  two tabs refreshing at once signed the user out. Verified: setting
  `GRACE_SECONDS = 0` fails exactly one test and no others. Reuse *after* the
  window is treated as theft and revokes the whole token family.
- **`app.core.security` accepts `pbkdf2_sha256`** because every user migrated
  from the old backend has one. A successful login re-hashes to bcrypt. Do not
  drop the legacy scheme until the migration is long done — dropping it early
  locks out every pre-cutover account.
- **Unverified users can still sign in.** Blocking login on email verification
  locks someone out of an account when mail is slow or bounces. Status is on
  `/me`; gating risky actions belongs to the modules that own them.
- **Identity never sends email.** It issues a token and hands it to a `Notifier`
  (`app/core/notifier.py`). Wiring a provider into the auth module would make
  every test that registers a user depend on it.
- **The api layer may not import a module's `models`.** Entity types come from
  the package surface (`from app.modules.identity import User`). Enforced by the
  `api-does-not-touch-orm-models` contract, which allows indirect imports —
  calling a service that uses its own models is fine.

- **Kiosk access goes through one `Scope`.** `kiosks.scope.kiosk_scope(db, actor)`
  is the only place that decides which kiosks someone may touch, and every
  repository read takes the result as its first argument. There is no unscoped
  read. Admin is the same resolver returning a wider scope — **not** a second
  router, which is what `/owner/*` was in the old backend, complete with a
  "DO NOT LOOSEN" comment where a check should have been.
- **Out of scope is 404, never 403.** A 403 confirms the kiosk exists, telling
  one shop owner something true about a competitor's estate. The message is
  byte-identical to a kiosk that never existed.
- **A refiller's response type has no money field to populate.** Enforced by
  `RefillerKioskResponse` rather than by remembering to strip prices.
- **Two placeholder seams fail in opposite directions, deliberately.**
  `PlatformOnlyBilling` fails *closed* — no SOLD/SAAS kiosk can go live until
  the payments module lands, because a misrouted payment is silent and
  irreversible. `PlatformBand` fails *open* — an unbounded price band lets an
  owner set a silly price, which is visible and reversible. Do not "fix" either
  to match the other.
- **A refund's destination is read off the Payment, never re-derived.**
  `collecting_user_id` is the gate's answer recorded at checkout. Platform money
  may go to the wallet or back to source; an owner's money may only go back to
  source, because the platform cannot credit a balance against rupees it never
  held. The old backend had `refunds` as one of three services independently
  deciding whose Razorpay collects.
- **The refund idempotency key is looked up before any validation.** Not after.
  A fully-refunded payment is REFUNDED with nothing left to give back, so a
  retry validated first is refused for exceeding the captured amount -- and the
  caller, told their refund failed when it had succeeded, issues another with a
  fresh key. Verified: moving the lookup below the checks fails exactly two
  tests. The same key is passed to Razorpay, so both sides agree on "done".
- **`payments` must not import `orders`.** Orders imports payments, so the
  refund's effect on an order comes back through the `RefundSink` protocol,
  wired at the composition root. Enforced by the
  `payments-does-not-know-what-it-paid-for` contract rather than by the sentence
  in the module docstring that used to be the only thing holding it.
- **Enum columns use `core.db.EnumText`.** A `Mapped[SomeEnum]` column typed as
  a bare `String` returns a plain `str` after a database round-trip. These are
  StrEnums, so `value == Enum.X` still passes and tests stay green, while
  `value.value` raises `AttributeError`. The annotation must not lie.

## How this work is done

Read this before continuing the build. It is the method, not a preference.

1. **TDD.** Test first, watch it fail for the right reason, then implement.
2. **Mutation-test anything that matters.** After a security or money rule
   lands, deliberately break it and confirm tests fail; then restore. A guardrail
   that has never been seen to fail is not known to work — three times in this
   build a "passing" check turned out to be inspecting an empty set.
3. **One rule, one implementation, one mechanism.** Every defect in the legacy
   audit is a rule enforced in one place and not another. If a rule cannot be
   made mechanical, say so rather than writing a comment asking people to be
   careful.
4. **Verify, don't assume.** Run the command, read the output. Check claims
   against the old backend's source before repeating them — one "fact" in this
   spec was stale and had to be corrected.
5. **Say what is not done.** Partial work is reported as partial.

## State of play

**929 tests passing, 10 import contracts kept, ruff clean.** Verify with:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

### Built

| Area | What exists |
|---|---|
| `app/core/` | config, db (+`EnumText`), ids, money, errors, crypto, security, notifier |
| `identity/` | users, roles, sessions with rotation + reuse detection, password/Google/guest sign-in, email verification, password reset |
| `kiosks/` | registry, types, onboarding + LIVE gate, pricing bands, paper (incl. consumption from device-reported sheets), assignments, consent-based staff invites, **the scope resolver** |
| `payments/` | owner Razorpay keys encrypted at rest, set-once with approval, **the payment gate**, checkout + capture, in-house signature verification, one webhook per collecting account, **refunds** |
| `billing/` | plans, subscriptions, trials, per-owner discounts (D13), one quote function |
| `orders/` | **the aggregate** — payment and print tasks commit together, so "paid but never printed" is unreachable; quotes + gateway fee, wallet and gateway as two branches into one commit, expiry that releases reserved paper |
| `wallet/` | ledger-as-record with the balance derived from it, double-spend refused by a conditional UPDATE rather than a read-check-write, `UNIQUE (wallet_id, reference)` for replayed webhooks |
| `printing/` | print options + the one workload calculation, Document and PrintTask models, **atomic claim with `FOR UPDATE SKIP LOCKED`** and lease recovery, storage (opaque keys), PDF pipeline (Ghostscript under `-dSAFER`), task progress + paper from device-reported sheets, photo→A4 layout, retention |
| `api/` | `deps`, `student/auth|staff|documents`, `owner/kiosks`, `refiller/kiosks`, `device/agent|tasks` — 45 routes, all in `tests/authz/matrix.py` |

### Not built yet, in dependency order

1. **Refunding a wallet-paid order.** Gateway refunds are done. A wallet-paid
   order writes no `Payment` row -- `pay_with_wallet` debits and sets
   `payment_reference` to the order's public id -- so `payments.refund` cannot
   reach it, and today such an order cannot be refunded at all. Deciding where
   that reversal lives is an open design question, not an oversight.
2. **ops** — admin alerts, audit, analytics, exports
3. **admin API layer** — `/v1/admin/*`
4. **device WebSocket hub** — needs Redis, deferred to staging by the operator
5. **migration** from `printit_legacy` (restored locally from a prod dump)
6. **cutover** — agent rewrite, staging, freeze window

### Blocked on the operator

- **Redis** — not installed. Confirmed by the operator as a production concern,
  not a local one. It is only needed for the device WebSocket hub so production
  can run >1 worker; the device API works without it by polling. Tests will use
  `fakeredis`; real Redis is exercised at staging.
- **Three data decisions** before the migration can be written — see the end of
  `docs/superpowers/specs/2026-08-15-legacy-data-audit.md`: the ownerless SOLD
  kiosk, the case-duplicate accounts, and the test/duplicate kiosks.

### Reference documents

- `docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md` — the design,
  13 recorded decisions, and **§2a mapping every legacy defect to the mechanism
  that prevents it**
- `docs/superpowers/specs/2026-08-15-legacy-data-audit.md` — what is actually
  wrong in the production data
- `docs/superpowers/plans/` — per-sub-project plans, each with an outcome
  section listing the defects found while building it
