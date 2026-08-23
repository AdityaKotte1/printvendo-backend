# printvendo-backend

The rebuilt central API. Replaces `cloud-backend/`, which stays deployed and
serving production until cutover. See
`docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md` for the design
and the reasoning behind everything below.

## Stack

Python 3.12, FastAPI, SQLAlchemy 2.0, Postgres, Alembic. Redis carries the
device wake and, anywhere with more than one worker, the rate limiter's counts;
dev runs without it and both degrade rather than break.

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
- **Workers may exceed 1.** The device WebSocket hub routes through Redis
  pub/sub, not a per-process dict — the constraint the old backend could never
  lift. `app/core/bus.py`.
- **The socket carries a wake, never work.** "Something is queued" goes down the
  wire; the device then claims over the ordinary HTTP path, which is one
  `FOR UPDATE SKIP LOCKED` statement. Sending the task itself would be a second
  implementation of claiming, and a reconnect overlapping a publish could hand
  one job to two devices. Polling remains the fallback and still works.
- **A wake is marked where work is created and sent after the commit.**
  `enqueue_task` calls `mark_for_wake`; `get_db` flushes once the transaction
  has committed. No route sends one, so no route can forget — and a device woken
  before the commit would ask, see nothing, and have spent its notification.
- **The authz matrix covers WebSocket routes too**, under the pseudo-method
  `WS`. A route with no `methods` would otherwise have been collected by
  nothing.
- **`legacy_id` is set by the migration and by nothing else.** It is a
  parameter of `create_kiosk`, not a field on any request schema: a route that
  let an admin claim a legacy id by hand would let somebody attach a stranger's
  order history to a kiosk they had just created.
- **The migration creates through the services; it does not copy rows.** Kiosks
  come from `create_kiosk` and climb the onboarding ladder, so every invariant
  applies to imported data and a SOLD kiosk still cannot reach LIVE without a
  working payment gate. Nothing learned from the old dump is hardcoded — the
  decisions are rules applied to whatever the fresh dump contains.
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
- **Every paid order has exactly one `Payment`, however it was paid.**
  `pay_with_wallet` debits the ledger *and* writes a CAPTURED `Payment` with
  `source = WALLET`, in the same transaction, so a refused debit leaves neither.
  The alternative — a wallet reversal living in `orders` — would mean two
  implementations of "how much of this has been given back" and two of "is it
  fully refunded yet", which is the shape of every defect in the legacy audit.
- **`Payment.source`, not `Payment.gateway`.** A column called `gateway` holding
  `"wallet"` is `price_cents` holding rupees. `PaymentSource` is deliberately
  *not* `Gateway`: the gate answers "whose Razorpay collects at this kiosk", and
  WALLET is not an answer it can give. `Order.gateway` still records the gate's
  answer and is **not** what a refund reads.
- **The refund destination table is derived, never flagged.** Wallet is legal
  when `collecting_user_id is None`; source is legal when
  `razorpay_payment_id is not None`. Two reads off the row cover all three
  cases: balance-paid (wallet only — there is no gateway payment to reverse),
  platform-collected (either), owner-collected (source only). Mutation-tested.
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
- **A proof of account ownership is bytes from an authenticated route.** Never a
  URL. The old admin dashboard built `API_BASE + '/storage/...'` behind an
  `onerror` handler, so a proof that failed to load looked exactly like one
  nobody had uploaded and bank-detail changes were approved unseen. A request
  with no proof is a 404 saying so. Inline rendering is limited to an allowlist
  of image and PDF types; anything else downloads, because an SVG served inline
  from the API's origin is stored XSS uploaded by the person under review.
- **Admin is a wider scope, never a bypass.** `/v1/admin/kiosks/{id}/stage`
  covers the whole onboarding ladder where the owner's route covers two rungs --
  but it is the same `move_to`, so CONFIGURED still cannot be skipped and a SOLD
  kiosk whose owner cannot collect still cannot go LIVE.
- **An admin cannot disarm themselves.** Revoking your own admin role or
  deactivating your own account is refused; another admin may do both. There is
  no second surface that could grant either back.
- **Platform revenue has four buckets and no total.** Ours, the owners', 
  subscription income, and student balances we merely hold are four kinds of
  money; a sum across them is true of nothing, and counting top-ups as takings
  books a liability as income. How much of our bucket is owed onward to owners
  is deliberately unanswered here -- it is a question about kiosk ownership, and
  payments does not know.
- **A trial is a money-routing lever.** A subscription inside its trial is in
  force, which is half of what the payment gate requires, so granting one turns
  a shop's takings on. A second grant extends the first rather than adding a
  row: two live trials would make "when does this stop being free" a question
  with two answers, and `active_subscription` takes the longest-running one.
- **A page is one side of a sheet, and money is charged by the sheet.** A
  `_single` price is a sheet printed on one side; a `_double` price is a sheet
  printed on both. Two pages share a duplex sheet, and an odd page finishes on
  a sheet printed on one side — charged as one, because its back is blank.
  `quote_line` multiplied the double rate by the number of *sides*, which made
  duplex dearer than simplex while using half the paper; three parts of the
  system disagreed about the unit, including the module's own tests. The
  property is now stated as one: **duplex never costs more than the same job
  single-sided.** `lib/price.ts` mirrors `quote_line` exactly, verified across
  96 combinations, because a pay screen whose number changes at checkout is
  worse than no estimate.
- **The authz matrix is exercised, not merely declared.**
  `tests/authz/test_matrix_enforced.py` fires every audience the matrix does
  *not* name at every route and requires a refusal — 401, 403 or 404. A **422
  fails**, because it means the caller reached body validation and only the
  shape of their request stopped them. It found eight routes guarded by scope
  alone, where a signed-in student reached the handler and got an empty list:
  nothing leaked, and the matrix's claim was false. Role guards live on the
  *router*, so a route added tomorrow inherits one.
- **A refused delete must not destroy the file first.**
  `delete_document` removed the bytes and then let the database refuse the row,
  answering 204 while deleting nothing — the student was told their file was
  gone, the list still showed it, and it could no longer be printed. Every
  reason to refuse is checked before anything is touched, and "is an order
  counting on this?" arrives through a `DocumentUse` protocol wired at the
  composition root, because printing may not import orders.
- **A subscription is quoted once, frozen, and extends rather than overlaps.**
  A plan's price may change while an owner is at the payment page. A renewal
  starts when the current *entitlement* ends — the paid term or a trial running
  past it — never at the end of the grace window, which is a buffer against a
  late renewal and not time anybody bought. Nothing is in force until the money
  arrives, and settling is idempotent because the webhook and the browser both
  settle the same capture.
- **A refund's idempotency key is required, never generated server-side.** A
  request that times out is retried with the same key and gets back the refund
  it already made. Inventing one per request was mutation-tested and fails the
  retry test, which is exactly the defect it would be.
- **Provisioning is a use case, not a shortcut.** `app/provisioning.py` climbs
  the same onboarding ladder through the same services and stops where the
  rules stop it; what it adds is *saying why*, in sentences. Its reasons are
  staged — until somebody owns a shop there is nobody whose subscription or
  keys could be missing. It sits below the composition roots so the admin route
  and the command line run one implementation.
- **Rate limits are a table, not a decorator.** `app/api/ratelimit.py` holds
  (method, path) → windows and an ASGI middleware applies them before routing,
  so a refused request costs no session and no query. A decorator per route is
  a thing somebody has to remember, and the one nobody added looks exactly like
  a route that was considered and left open. Coverage is derived from
  `tests/authz/matrix.py`: every route callable without a credential must
  appear in `LIMITS` or in `UNLIMITED` **with a reason**. The limits are
  per-address and campus-NAT-aware — two hundred students share one IP — so
  they bound a script, not a person; per-account limits need the email out of
  the body and do not exist yet.
- **The limiter fails open, the payment gate fails closed.** A Redis outage
  that refused every login would be an outage of the product to protect it from
  an abuse that may not be happening, and nothing the limiter guards is
  unguarded by a password or a signature.
- **`/health` runs `select 1`.** A probe answering 200 from the framework alone
  answers the one question nobody is asking. 503 when the database is
  unreachable, because the thing reading it is a load balancer.
- **`app/jobs/` is a composition root, like `app/api`.** It may know several
  contexts at once — the paper watcher reads kiosks and writes an ops alert —
  but reaches them through their surfaces, and the two roots may not import
  each other (`app.jobs | app.api`; the pipe means *independent*, a colon does
  not, and that was verified rather than assumed).
- **Every worker runs the scheduler; the advisory lock decides.** One
  `pg_try_advisory_lock` per job, never a blocking acquire — queueing the other
  three workers would run the sweep four times in a row, which is the thing the
  lock is for.
- **A watcher that raises must stand down.** `ops.resolve_by_key` closes the
  alert when the condition clears, with no actor recorded: "it stopped on its
  own" is a different fact from "somebody dealt with it". Otherwise the console
  fills with shops that were briefly offline last week, which is the wall of
  unread notifications the alerts table exists to avoid.
- **A background sweep reads through `kiosks.system_scope()`.** One named way
  to say "not on anybody's behalf". Each caller writing
  `Scope(is_unrestricted=True, …)` inline would be the old backend's admin
  bypass, spelled differently in every place it appears.
- **An export is refused rather than truncated.** Past `MAX_EXPORT_ROWS` the
  owner CSV returns a sentence asking for a shorter period. A short accounting
  file is a wrong number that looks exactly like a right one. It windows on
  `paid_at` so it reconciles with `/v1/owner/earnings`, and it carries no
  filenames — "Medical Results Ravi Kumar.pdf" names a person as surely as an
  address does.
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

## Picking up a session

`HANDOFF.md` has the exact commit work stopped at, what to start on, and the
traps that cost time last session. Read it after this file. Update it at the end
of a session and delete anything that has become true here instead — two
documents describing the same thing is how they drift.

## State of play

**1552 tests passing, 105 routes, 12 import contracts kept, ruff clean.** Verify with:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

### Built

| Area | What exists |
|---|---|
| `app/core/` | config, db (+`EnumText`), ids, money, errors, crypto, security, notifier |
| `identity/` | users, roles, sessions with rotation + reuse detection, password/Google/guest sign-in, email verification, password reset |
| `kiosks/` | registry, types, onboarding + LIVE gate, pricing bands, paper (incl. consumption from device-reported sheets), assignments, consent-based staff invites, **the scope resolver** |
| `payments/` | owner Razorpay keys encrypted at rest, set-once with approval, **the payment gate**, checkout + capture, in-house signature verification, one webhook per collecting account, **refunds** (gateway and balance, one path), now reachable over HTTP |
| `billing/` | plans, subscriptions, trials, **purchase and renewal**, per-owner discounts (D13), one quote function |
| `orders/` | **the aggregate** — payment and print tasks commit together, so "paid but never printed" is unreachable; quotes + gateway fee, wallet and gateway as two branches into one commit, expiry that releases reserved paper |
| `wallet/` | ledger-as-record with the balance derived from it, double-spend refused by a conditional UPDATE rather than a read-check-write, `UNIQUE (wallet_id, reference)` for replayed webhooks |
| `printing/` | print options + the one workload calculation, Document and PrintTask models, **atomic claim with `FOR UPDATE SKIP LOCKED`** and lease recovery, storage (opaque keys), PDF pipeline (Ghostscript under `-dSAFER`), task progress + paper from device-reported sheets, photo→A4 layout, retention |
| `ops/` | audit trail (one rule, matrix-enforced) and deduplicating admin alerts that stand down when the condition clears |
| `api/` | `deps`, `student/*`, `owner/*` (incl. the orders CSV), `refiller/kiosks`, `device/*` (incl. **the WebSocket**), **`admin/*`**, **rate limits** — 97 routes, all in `tests/authz/matrix.py` |
| `jobs/` | the scheduler and four sweeps: order expiry, file retention, the offline-kiosk watcher, the paper watcher |
| `cli/` | `bootstrap-admin`, `seed`, `provision-kiosk` — the first way in, and a world to click through |
| `provisioning` | one use case, two roots: stand a kiosk up and say what is still missing |

### Not built yet, in dependency order

1. **deploy** — there is no `deploy/`, and `docs_url=None` in production belongs
   with it
2. **migration** from `printit_legacy` (a dump taken during a maintenance
   window; the local restore is gone)
3. **cutover** — staging, freeze window

The device agent is **built**: `../printvendo-agent`, one agent for both a
Raspberry Pi and a Windows PC, on `/v1/device/*`. It replaces `pi-agent/` and
`windows-agent (1)/`, which stay in the repo until cutover and must not be
edited. It polls rather than holding the wake socket, which works and is slower.

Not modules, but real and still unowned: push notifications, the paper-shop
catalogue, the owner console, and per-account rate limits (as opposed to
per-address ones). See `HANDOFF.md`.

### Blocked on the operator

- **Redis** — not installed. Confirmed by the operator as a production concern,
  not a local one. The device API works without it by polling; the socket is
  what needs it. Tests run against `fakeredis`, which speaks the same pub/sub
  protocol, so `RedisBus` itself is exercised rather than a stand-in. Real Redis
  is exercised at staging.
- **A fresh production dump.** The three data decisions were answered on
  2026-08-22 and are recorded, as rules, at the end of
  `docs/superpowers/specs/2026-08-15-legacy-data-audit.md`. What is now missing
  is the data: the local `printit_legacy` restore is **gone** (that database and
  three others exist with zero tables), so the legacy *schema* comes from
  `cloud-backend/app/models/` and the *rows* arrive as a `pg_dump` taken during a
  maintenance window. Every figure in the audit is illustrative, not current.

### Reference documents

- `docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md` — the design,
  13 recorded decisions, and **§2a mapping every legacy defect to the mechanism
  that prevents it**
- `docs/superpowers/specs/2026-08-15-legacy-data-audit.md` — what is actually
  wrong in the production data
- `docs/superpowers/specs/2026-08-22-student-app-api-gap.md` — every call
  `printvendo-web` makes, where it lands here, and what has no home on either
  side (favourites, the paper shop, push, invoices, and three pages the
  outgoing emails already point at)
- `docs/superpowers/plans/` — per-sub-project plans, each with an outcome
  section listing the defects found while building it
