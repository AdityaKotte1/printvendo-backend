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
- **The authz matrix is exercised in both directions.**
  `tests/authz/test_matrix_enforced.py` fires every audience the matrix does
  *not* name at every route and requires a refusal — 401, 403 or 404. A **422
  fails**, because it means the caller reached body validation and only the
  shape of their request stopped them. It found eight routes guarded by scope
  alone, where a signed-in student reached the handler and got an empty list:
  nothing leaked, and the matrix's claim was false. Role guards live on the
  *router*, so a route added tomorrow inherits one.

  It now also fires every audience the matrix **does** name and requires that
  they are *not* turned away: 404 and 422 are fine — the caller reached the
  handler — while 401 and 403 mean the matrix is lying. That half was missing,
  and its absence is how `/v1/owner/earnings*` came to 403 an admin the matrix
  promised, across four routes, with a green build throughout. Verified by
  reintroducing exactly that narrowing and watching the test name the route and
  the audience. A PUBLIC route is held only to *not 403*: several carry a
  credential of their own — a webhook signature, a refresh cookie — and are
  right to answer 401 without it, so demanding a success there would be
  demanding the wrong thing.
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
  **two buckets, and which one is tight depends on what is known.** A verified
  bearer token is counted against its *account* at the table's numbers and
  against its address at `ADDRESS_FANOUT` times them — so one student's script
  spends its own budget rather than the lecture hall's, while a machine
  rotating claims still hits a wall. The token must be **verified**: keying on
  an unchecked `sub` is keying on a field the caller controls, which is not a
  limit. Mutation-tested with a well-formed, wrongly-signed token, because
  garbage like `Bearer not.a.token` is rejected by an unverified decoder too
  and would have proved nothing. Sign-in stays per-address, unavoidably and
  correctly: there is no token yet, the account is in a body this middleware
  deliberately does not read, and credential stuffing rotates accounts from one
  machine anyway.
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
- **An admin sees a whole order; an owner never can.**
  `GET /v1/admin/orders/{id}` carries the student's account id, address and
  name, the payment's source and Razorpay ids, whose account collected, and
  every refund already issued. `OwnerOrderResponse` has none of that and must
  not grow it — that absence is what makes the owner routes incapable of
  leaking identity however they are later edited, so admin gets a **wider type
  on its own route** rather than a widened one. The audience is the control
  here, not the scope: an owner reading this at a shop they hold would be
  handed exactly what their own surface exists to withhold, so the route is
  ADMIN-only and both an owner and a student are refused.
- **A role may be listed; STUDENT may not.** `GET /v1/admin/accounts` takes an
  exact address *or* a role, and refuses `student`. Owners, refillers and
  admins are the handful of people an operator administers, already named on
  kiosks the same admin can list -- making somebody remember an address per
  shop buys nothing. Students are the directory the exactness rule exists to
  prevent, and saying so in the refusal beats letting an operator conclude the
  console is broken. Naming neither is refused too: it is a request that has
  not said what it wants, not a request for everybody.
- **A restart nobody would carry out is refused, not reported.**
  `restart_agent` reports success *before* acting, because the process that
  would report it afterwards is the one being killed -- so it first asks
  whether anything supervises it (`systemctl is-enabled`, `Get-ScheduledTask`).
  An agent started by hand would otherwise detach a command that does nothing
  while the console said "succeeded" about a shop that never came back. The
  check runs as a `precheck` on the handler, before the caller commits to the
  report, because a refusal raised afterwards is a refusal nobody hears.
- **A device command is a row, and it expires.** Restarting the machine in a
  shop goes through one route both an owner and an admin reach
  (`POST /v1/owner/kiosks/{id}/device/commands`) -- the old backend had
  `/kiosk/printers/{id}/restart` for an owner and a second copy in `pi.py` for
  an admin, and they drifted. The machine claims it over HTTP like a print
  task, because the socket carries a wake and never work. Unlike a print task
  it goes stale: a restart asked for at four and run at five restarts a shop
  that has been printing again for an hour. Asking twice while one is waiting
  returns the first, because a button that appears to do nothing gets pressed
  again.
- **`restart_printing`, never `restart_cups`.** It is CUPS on a Pi and the
  Print Spooler on Windows; a name that is true on half the estate is
  `price_cents` holding rupees. There is deliberately no Ghostscript command --
  a copy runs for one file and exits, so a button would be a placebo.
- **A stuck printer closes the shop, and only reopens one it closed.** The
  agent tells `/v1/device/printer-health` when a job will not come out; the
  kiosk moves to MAINTENANCE, which `is_selling` already excludes, so students
  stop being offered it while every operator surface still shows it and says
  why. `KioskDevice.stuck_since` is what says *we* are the reason: an owner who
  put their own shop into maintenance to change a cartridge must not have it
  reopened by a queue clearing. A file Ghostscript refuses is not this --
  `PrinterStuck` is a type in the agent precisely so one student's bad PDF
  cannot close a working shop. **`stuck_since` is written only where the shop is
  actually closed**, and the order of those two checks is the whole guarantee:
  it used to be set before the stage was read, so a jam at a shop an owner had
  already put into maintenance recorded us as the reason -- and the next
  recovery handed that shop back to students with the printer in pieces. The
  matching release in `report_recovered` is *un*gated on purpose, so a claim is
  never left on a shop somebody else has already reopened.
- **A refund has one implementation and two doors.** `app/refunding.py` is the
  use case; `/v1/admin/orders/{id}/refund` reaches every order and
  `/v1/owner/kiosks/{id}/orders/{id}/refund` reaches the orders at kiosks the
  caller holds. The difference is *which orders are visible* and nothing else --
  the old backend had a refund in `kiosk.py` for an owner and a second in
  `refunds.py` for an admin, each deciding independently whose Razorpay
  collects, and that is how student money reached the wrong account. The kiosk
  in the owner path is checked rather than decorative: an order at one of the
  caller's *other* shops is 404 there, exactly as a stranger's is.
- **A shop gives back money its own account collected.** One check --
  `own_takings_only`, set by the owner door and not by the admin's -- with two
  consequences that are never enforced a second time. The money comes back out
  of the *owner's* Razorpay, because `credentials_for_payment` reads the same
  `collecting_user_id`; and it can only go to the source, because a balance
  refund is legal only where nobody else collected, which the check has just
  refused. `OwnerRefundRequest` therefore has **no destination field at all** --
  the question cannot be asked, so it cannot be answered wrongly. Platform
  takings and balance-paid orders are refused there with a sentence naming who
  to ask, and go back through the admin door instead.
- **The owner refund route is the one place admin is not alongside.**
  Everywhere else in `/v1/owner/*` admin is a wider kiosk scope through the
  same route. This one is not about scope: an admin has collected nothing, so
  the rule above could only ever refuse them. A 403 at the door says that; a
  409 at the money would read as a bug.
- **An invoice is the same document every time it is downloaded.** The number
  is derived from the subscription (`PV-SUB_…`) and the date is read off the
  capture, never `now`. A counter would have to survive a rollback and be
  unique across the estate; the public id already is both, and a number you can
  look the subscription up by is worth more than one that counts. It exists
  only for money that arrived -- a pending purchase is a quote and a granted
  trial cost nothing, and "TOTAL PAID" on either is a document somebody can
  wave at a shop.
- **Two documents, one letterhead.** `app/core/documents.py` holds the band,
  the rule and the palette; `orders` renders a student's receipt and `billing`
  an owner's invoice on top of it. They are separate bounded contexts and may
  not import each other, so without a home in core the brand would exist twice
  and drift -- and a shop would hold two papers from the same company that did
  not look like the same company. Chrome only: the moment that file grows a
  `total` it has become a second opinion about money.
- **One refund, one audit entry.** The idempotency key is what makes a retry
  safe, and `refund` returns the existing row either way -- so whether this is a
  first attempt is asked *before* the refund, never after. Recording
  unconditionally wrote `payment.refunded` twice for one refund, which an
  operator reading the trail cannot tell from two refunds. This trail is the
  only record there is: owners are paid directly, so there is no settlement run
  in which the discrepancy would surface. Found by retrying the route, not by
  reading it.
- **A day ends where the shop is.** `earnings_by_day` buckets on
  `captured_at AT TIME ZONE 'Asia/Kolkata'`, named once as
  `REPORTING_TIMEZONE`. Every timestamp is stored in UTC, and a sale at half
  past eight in the evening UTC happened at two the next morning in Karnataka --
  bucketing in UTC files a late sale under the day before, and a shopkeeper
  reconciling yesterday finds a figure matching nothing they saw.
- **The day series is a query, not a chart.** `/v1/owner/earnings/daily` is the
  same predicate and the same columns as the window total, grouped by day, so
  the bars sum to the figure printed above them. The admin console used to add
  one up client-side from the order export -- which caps at a row count,
  buckets in UTC and cannot see a refund -- and the owner app was about to
  build a second. Both now read this one. A quiet day inside the series is a
  zero rather than a gap, because a missing day renders as a bar next to the
  wrong neighbour; the series is *not* padded to the ends of the window, since
  an owner asking about this year in March does not want nine months of empty
  bars.
- **A subscription settles from the browser as well as the webhook.**
  `POST /v1/owner/billing/subscription/{id}/verify` checks the signature
  against the **platform** key -- a subscription is always collected by the
  platform, and reading the owner's keys there would let a shop sign its own
  subscription into force. Both paths settle the same capture and the second is
  refused by the unique payment id. Until it existed the webhook was the only
  path, so an owner who had just paid sat on a page saying "not active" for as
  long as the delivery took, or for ever if their endpoint was wrong -- which
  is how somebody comes to pay twice.
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

**1704 tests passing, 116 routes, 12 import contracts kept, ruff clean.** Verify with:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

### Built

| Area | What exists |
|---|---|
| `app/core/` | config, db (+`EnumText`), ids, money, errors, crypto, security, notifier, documents |
| `identity/` | users, roles, sessions with rotation + reuse detection, password/Google/guest sign-in, email verification, password reset |
| `kiosks/` | registry, types, onboarding + LIVE gate, pricing bands, paper (incl. consumption from device-reported sheets), assignments, consent-based staff invites, **the scope resolver** |
| `payments/` | owner Razorpay keys encrypted at rest, set-once with approval, **the payment gate**, checkout + capture, in-house signature verification, one webhook per collecting account, **refunds** (gateway and balance, one path), now reachable over HTTP |
| `billing/` | plans, subscriptions, trials, **purchase and renewal**, per-owner discounts (D13), one quote function, **the subscription invoice** |
| `orders/` | **the aggregate** — payment and print tasks commit together, so "paid but never printed" is unreachable; quotes + gateway fee, wallet and gateway as two branches into one commit, expiry that releases reserved paper |
| `wallet/` | ledger-as-record with the balance derived from it, double-spend refused by a conditional UPDATE rather than a read-check-write, `UNIQUE (wallet_id, reference)` for replayed webhooks |
| `printing/` | print options + the one workload calculation, Document and PrintTask models, **atomic claim with `FOR UPDATE SKIP LOCKED`** and lease recovery, storage (opaque keys), PDF pipeline (Ghostscript under `-dSAFER`), task progress + paper from device-reported sheets, photo→A4 layout, retention |
| `ops/` | audit trail (one rule, matrix-enforced) and deduplicating admin alerts that stand down when the condition clears |
| `api/` | `deps`, `student/*`, `owner/*` (incl. the orders CSV, **device commands**, **the day series** and **an owner refund**), `refiller/kiosks`, `device/*` (incl. **the WebSocket**, **commands** and **printer health**), **`admin/*`**, **rate limits** — 114 routes, all in `tests/authz/matrix.py` |
| `jobs/` | the scheduler and four sweeps: order expiry, file retention, the offline-kiosk watcher, the paper watcher |
| `cli/` | `bootstrap-admin`, `seed`, `provision-kiosk` — the first way in, and a world to click through |
| `provisioning` | one use case, two roots: stand a kiosk up and say what is still missing |
| `refunding` | one use case, two doors: the admin's, over every order, and the owner's, over their own shops' |

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

The **admin console** is built: `../printvendo-admin`, three static files, no
build step. It reaches every admin route plus the owner routes admin shares.
`/docs` is no longer the admin surface.

The **owner console** is on this API: `../printvendo-owner`, rewired off
`cloud-backend`. Its own `CLAUDE.md` lists what was dropped rather than built.

Not modules, but real and still unowned: push notifications, the paper-shop
catalogue, and per-account rate limits (as opposed to per-address ones). See
`HANDOFF.md`.

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
