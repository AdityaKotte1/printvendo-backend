# Handoff

Read `CLAUDE.md` first — it carries the conventions and the state of play, and
it is loaded automatically. This file carries only what CLAUDE.md does not: the
exact point work stopped, and the traps that cost time.

**Last updated: 2026-08-24.** The owner app is off the legacy API. Five
endpoints were built for it, two of them not on the original list. An owner may
now refund **only money their own Razorpay collected**, and a paid subscription
has a printable invoice.

**Nothing below is committed.** Four working trees are dirty —
`printvendo-backend`, `printvendo-agent`, `printvendo-owner`, and
`printvendo-admin`, which is still not a git repo. `git status` in each is the
first thing to look at. Everything passes; it simply has not been committed.

Update this file at the end of a session. Delete anything that has become true
in CLAUDE.md — two documents describing the same thing is how they drift.

---

## Where things stand

```
1674 tests passing · 12 import contracts · ruff clean · 115 routes
   + 78 agent tests, ruff clean
   + printvendo-owner: typecheck clean, static export builds
```

Verify before trusting that line:

```bash
.venv/Scripts/python -m pytest -q
PYTHONIOENCODING=utf-8 .venv/Scripts/lint-imports    # see the traps
.venv/Scripts/python -m ruff check .
cd ../printvendo-agent && ../printvendo-backend/.venv/Scripts/python -m pytest -q
cd ../printvendo-owner && npm run typecheck && npm run build
```

Five components are current, and **every console is now on this API**:

| Repo | State |
|---|---|
| `printvendo-backend` (here) | one refund with two doors, the day series, subscription verify |
| `printvendo-agent` | unchanged |
| `printvendo-admin` | its day chart now reads the endpoint instead of adding up a CSV |
| `printvendo-web` | `bf0a9be` — unchanged |
| `printvendo-owner` | **rewired** off `cloud-backend`, whole app |

---

## What was done this session

### Backend — three routes

- **`POST /v1/owner/kiosks/{id}/orders/{id}/refund`.** The shop can put it
  right at the counter; until now the only refund was admin-only, so the person
  standing in front of the student had to email somebody. It is the **same**
  refund as the admin's — `app/refunding.py`, a use case beside
  `app/provisioning.py` — and the admin route was rewired onto it, so there is
  one implementation and two doors. The kiosk in the path is checked: an order
  at one of the caller's *other* shops is 404 there, exactly as a stranger's is.
- **`GET /v1/owner/earnings/daily`.** The window total grouped by day, in
  `Asia/Kolkata`. `printvendo-admin` built one client-side out of the order
  export and `printvendo-owner` was about to build a second; both now read this.
- **`POST /v1/owner/billing/subscription/{id}/verify`** — *not on the original
  list, and built anyway*. The reason is in the next section.
- **`GET /v1/owner/billing/subscription/{id}/invoice`** — the printable
  invoice, as a PDF. Bytes from an authenticated route, never a URL. The
  number and the date are derived, so downloading it twice produces one
  document rather than two. `/v1/owner/billing` gained a `history` list so
  there is something to hang past invoices off — it only ever returned what was
  in force, which answers "am I covered" and not "where is last year's
  invoice".

### The owner refund got its rule

An owner may refund **only money their own Razorpay account collected**. One
check, two consequences that fall out rather than being enforced again: the
money comes back out of the owner's own account (`credentials_for_payment`
reads the same column), and it can only go to the source (a balance refund is
legal only where nobody else collected). `OwnerRefundRequest` has **no
destination field**, so the wallet cannot even be asked for.

That makes the owner door **OWNER-only** — the single place in `/v1/owner/*`
where admin is not alongside, because an admin has collected nothing and the
rule could only refuse them. Platform takings and balance-paid orders go back
through `/v1/admin/orders/{id}/refund`, which the admin console already uses.

**The consequence worth knowing:** at a PLATFORM kiosk an owner cannot refund
anything at all, and neither can they refund an order any student paid from
their balance. Both are Printvendo's money. If a shop should be able to hand
back platform takings, that is a product decision and a different mechanism —
it is not a relaxation of this one.

### `printvendo-owner` — the whole rewire

`lib/api.ts`, `lib/types.ts`, `lib/format.ts`, `lib/AuthContext.tsx` and every
page. Its own `CLAUDE.md` was rewritten and carries the decisions; the short
version:

- ids are opaque strings, money is a decimal string and **this app never does
  arithmetic on it**;
- silent refresh on a 401, one at a time — four parallel refreshes would rotate
  the token four times and rotation treats reuse as theft;
- `healthOf()` is built from the stage, `is_selling` and the heartbeat
  together, because "open" and "selling" are different questions and one status
  string could never tell them apart.

**Dropped, as agreed:** clear-queue and force-printed. Also `markOutOfPaper`
(refiller-only; no owner route) and `regenerateConfig` (replaced by an
enrolment code). Printable invoices were dropped and then **asked for back** --
they exist, as a PDF from the server rather than a sheet assembled in the
browser.

**Newly reached, all of it already built and previously unused:** refill logs,
device status / enrol / revoke, device commands, `earnings/by-kiosk`, the
billing quote, buying a subscription, all four payment-config routes,
change-password, and **accept-invite** — which is how an owner comes to hold a
kiosk at all and had no surface anywhere.

**Bank details are gone**, replaced by the payment-key change request with
proof upload. Printvendo never holds an owner's print money, so there was no
payout for an account number to be the destination of.

### The thing that was not asked for

Wiring "buy a subscription" turned up a **dead end**: nothing settled a
subscription except the Razorpay webhook. `app/api/deps.py` even said "the
webhook and the browser coming back settle the same payment" — the browser had
no route to come back to. An owner would pay, watch the page say *not active*,
and pay again. So `.../subscription/{id}/verify` exists now, mirroring
`/v1/app/orders/{id}/verify`: signature checked against the **platform** key,
because a subscription is always collected by the platform and reading the
owner's keys there would let a shop sign its own subscription into force.

### Found by running it, not by reading it

The invoice was fetched over real HTTP as the owner who bought it and read back
with `pypdf`: `PV-SUB_…`, the right dates, and arithmetic that works — 999 × 6
= 5,994, less 10% = 599.40, total 5,394.60. The refund refusals were driven the
same way: an owner asking to refund platform takings gets the 409 sentence, so
does one explicitly asking for the wallet, and an admin at the owner door gets
a 403.

The three new routes were driven over real HTTP against the dev database, and
the day series was checked to sum to the window total on real rows (it does:
₹204 + ₹281 = ₹485). Retrying the owner refund with the same idempotency key
turned up a bug the tests did not have: the money moved **once**, correctly,
and `payment.refunded` was written **twice**. An operator reading two entries of
₹1 has no way to tell that only one refund happened, and this trail is the only
record there is — owners are paid directly, so nothing else would surface the
discrepancy. Fixed in `app/refunding.py` by asking `refund_for_key` *before*
the refund; both doors got the fix, because both go through the use case.

### Mutation-tested, then restored

- `REPORTING_TIMEZONE = "UTC"` → fails exactly `test_a_day_ends_where_the_shop_is`.
- dropping the day series' gap fill → fails exactly the quiet-day test.
- dropping `order.kiosk_id != kiosk_id` in the owner refund → fails exactly
  the different-shop test.
- dropping `payment.subscription_id != subscription.id` in subscription verify
  → **failed nothing at first**. The test was reaching the `payment is None`
  branch instead, so it proved nothing; rewritten to present a genuine,
  correctly signed receipt from the owner's *own wallet top-up*, which is the
  actual attack. It fails now.
- signing the subscription callback with the wrong secret → fails two tests.
- recording the refund audit entry unconditionally → fails exactly the
  retry test above.
- dropping the `own_takings_only` check → fails exactly the two tests that
  refuse platform and balance money.
- giving `OwnerRefundRequest` a destination field back → fails exactly the test
  that says the wallet is unaskable.
- dating the invoice `now` instead of off the capture → fails exactly the
  issue-date test (which sets the term start a fortnight later, so a renderer
  reading the wrong field cannot accidentally print the right date).
- letting an unpaid subscription render → fails both the pending and the trial
  test.

---

## Start here

### 1. Deploy

Still the only thing between here and a shop taking real money:

- a `deploy/` that does not exist: compose, a proxy, TLS, backups;
- `docs_url=None` in production. `/docs` publishes all 115 routes to anybody.
  It is a **disclosure** problem, not a bypass — every admin route is behind
  `require_role(ADMIN)`, and no route grants a role without one. Free to close,
  so close it;
- `TRUST_PROXY_HEADERS`, or the whole internet shares one rate-limit bucket;
- real Redis: the device socket needs it, the limiter wants it past one worker;
- **three** static hosts now, and three origins in `CORS_ORIGINS`:
  `printvendo-web`, `printvendo-owner` (3002 in dev) and `printvendo-admin`.
  The admin console's API origin lives in `index.html` in **two adjacent
  lines** that must agree — `connect-src` and `<meta name="printvendo-api">`.
- **Razorpay's checkout script.** `printvendo-owner` loads
  `checkout.razorpay.com` for the subscription purchase. If that app ever gets
  a CSP, `script-src` has to name it — the admin console's meta-CSP trap,
  waiting to happen a second time.

### 2. Click the owner app through

**Nothing in `printvendo-owner` has been exercised against a running backend.**
Types, build and static export are verified; behaviour is not. That is the same
sentence its `CLAUDE.md` carried before the rewire, and it is still the truth.

Everything the admin console found last session was found by *using* it, not by
reading it — two real bugs in an afternoon. The owner app is bigger and has had
none of that. Start with:

```bash
.venv/Scripts/python -m uvicorn app.main:create_app --factory --port 8000
cd ../printvendo-owner && npm run dev      # 3002
```

`python -m app.cli seed` builds a world and prints the passwords. The paths
most likely to be wrong, in order: the **refund dialog** (destination rules are
the server's, and this app has never seen it refuse one), **the machine card**
(three routes and a state machine), and the **subscription purchase**, which
needs test Razorpay keys in `.env` to get past the checkout at all.

### 3. Two small mechanical jobs, both still open

**The authz matrix is only enforced in one direction.**
`tests/authz/test_matrix_enforced.py` fires every audience the matrix does
*not* name and requires a refusal. Nothing checks that a **named** audience
gets *through*. That is exactly how `/v1/owner/earnings*` came to 403 an admin
the matrix promised. The test already builds a credential per audience, so the
other direction is a small addition.

**`bootstrap-admin` can mint an account that cannot sign in.** It accepts any
string as an email; `POST /v1/app/auth/login` validates with `EmailStr`, which
rejects reserved TLDs. Validate the address where the CLI parses it.

---

## Traps that cost time in this session

**A 404 test passes before the route exists.** Four of the tests written here
were green against a route that had not been written, because "not found" is
also what FastAPI answers for a path it does not have. Assert the *sentence*,
or do something that must return 201 first, in the same test.

**A mutation test can pass for the wrong reason too.** Dropping the
subscription-id check broke nothing, because the test never got past the
earlier `payment is None` branch. A guardrail that has never been *seen* to
fail is not known to work — that is the third time in this build.

**A stale `.next` breaks the build with "Cannot find module for page".**
Deleting a page (`InvoiceSheet` went in this session) leaves the old manifest
behind, and prerender then fails for pages that are perfectly fine. `rm -rf
.next out` and rebuild; the error names the wrong thing entirely.

**`npm run lint` in `printvendo-owner` is not configured.** `next lint` drops
into an interactive setup prompt and hangs the tool. `typecheck` and `build`
are the checks that exist.

**`tsc --noEmit` can exit 0 having checked nothing** — it is incremental, and
`tsconfig.tsbuildinfo` was stale. Delete it, or prove the check is live by
adding a deliberate type error and watching it fail.

**Heredocs still fail on apostrophes** if the delimiter is unquoted. `<<'EOF'`
is fine and was used throughout; `<<EOF` is what breaks.

### Still true from last session

**`[hidden]` loses to your own `display`** — `[hidden] { display: none !important }`.

**A meta CSP needs `connect-src` spelled out.**

**A stale uvicorn broke two whole pages of the owner app**, and neither error
named it. Money died on a 404 for `/v1/owner/earnings/daily`; Account
*white-screened* on `Cannot read properties of undefined (reading 'length')`,
because the old server's `/v1/owner/billing` has no `history` field and the
card did `billing.history.length`. Restarting the server fixed both. The card
was fixed too -- `history` is now optional in the type and defaulted at the one
place that reads it, because a field this app added is a field an older server
does not send, and that is an ordinary state during a deploy rather than a
reason to white-screen. Check `Get-NetTCPConnection -LocalPort 8000` before
believing any owner-app bug.

**Uvicorn does not reload `.env`, and an old one may hold the port.**
`Get-NetTCPConnection -LocalPort 8000` finds the owner.

**A `precheck` has to run before the report, not inside the action.**

**`app.routes` no longer lists sub-routes.** Enumerate `app.openapi()["paths"]`.

**`/v1/admin/alerts` returns only what is still open.**

**`"$PSScriptRoot[windows]"` in PowerShell is an index into the path string.**

**The Windows agent runs as SYSTEM, and SYSTEM has its own printer list.**

**`python` on a fresh Windows PATH is the Microsoft Store stub.**

**The Windows spooler names every Ghostscript job "Ghostscript output".**

**`next build` while `next dev` is running breaks the dev server.**

**`tests/modules/identity/` is flaky in a full run, and it is not new.**
Four full-suite runs this session: two clean, two with the same five or so
failures in `test_models.py` / `test_guests.py` -- all of them around creating a
`User`, all `sqlalchemy` errors, all passing in isolation and passing on the
very next full run of the same code. Nothing in this session touches identity,
so it predates the work here. **Not root-caused.** Do not read a green run as
proof and do not read a red one as your change until you have run it twice.

**Do not run the suite while anything else is talking to Postgres.** Not just
a second pytest session: running the full suite in the background while a dev
uvicorn and a one-off script were up produced seventeen failures in `identity`
and `jobs` — table-level SQLAlchemy errors in tests that had nothing to do with
the change. Every one passed in isolation, and the whole suite was green the
moment nothing else was running. The mechanism was not chased down; the rule
is simply to run it alone before believing a failure.

**`tests/test_migrations.py` leaves the schema where the migration left it.**

**Autogenerated foreign keys are anonymous.**

**`git checkout <file>` is not how you undo a mutation test.** Copy the file
aside — three files were mutated this session and all three were restored from
a copy.

**`lint-imports > /dev/null` exits 1 whatever the contracts say.** Use
`PYTHONIOENCODING=utf-8`.

**import-linter: `|` means independent siblings, `:` does not.**
`app.provisioning | app.refunding` is one layer holding two independent use
cases.

---

## Left on the dev database by this session

- **A paid subscription** for `owner.demo-print-shop@demo.printvendo.com`
  (`sub_vpddf…`, Standard, 6 months, ₹5,394.60) with a fabricated captured
  payment `pay_smoke_3`. Written by hand to exercise the invoice; there is no
  Razorpay transaction behind it.
- **₹1 of owner-route refunds** on Agent Test Shop from the previous section.

## Two bugs found by using it, and fixed

**A failed subscription payment locked the owner out for ever.**
`PENDING_PAYMENT` was only ever set -- `activate_subscription` cleared it on
success and nothing cleared it on failure -- and no sweep looked at it. One
simulated-failure test payment meant that owner could never buy a subscription
again, which is the thing that keeps their shops selling. `ALREADY_BUYING` even
told them to "wait for it to lapse", and nothing made it lapse.
`PURCHASE_LIFETIME` (20 minutes, mirroring `ORDER_LIFETIME`) now bounds it, and
`start_purchase` cancels a lapsed one on the way past. Mutation-tested.

**Every successful print on Windows closed the shop** -- wherever the printer
has "Keep printed documents" set, and briefly on every job regardless.
`windows_state_from_jobs` knew `PRINTING` and the trouble bits, but not
`JOB_STATUS_PRINTED (0x80)` or `RETAINED (0x2000)`, so a finished job that the
spooler was still listing read as QUEUED. The wait then ran its full fifteen
minutes, `print_task` raised `PrinterStuck`, the task was reported FAILED with
the paper already in the student's hand, and `/v1/device/printer-health` moved
the kiosk to MAINTENANCE. `WINDOWS_DONE` is now dropped before anything else is
read, and `USER_INTERVENTION` joined the trouble bits -- "load paper in tray 2"
is a prompt nobody at a kiosk will answer. Mutation-tested; agent suite 85 pass.

## Known gaps that are not modules

- **No `deploy/`** — see *Start here*.
- **`printvendo-owner` has never been run against the backend.**
- **The authz matrix is enforced in one direction only.**
- **Rate limits are per address, not per account.**
- **No push notifications** — VAPID keys are read by nothing.
- **No paper-shop catalogue** — `ItemKind.SHOP_ITEM` exists and nothing else
  mentions it.
- **Nothing sweeps for unsettled payments** — the third watcher the alerts
  table was built for.
- **The agent does not connect the wake socket.** It claims commands on the
  same fifteen-second poll, so a restart can wait fifteen seconds.
- **A refund cannot be undone or listed.** There is no route that shows the
  refunds against a payment; the order simply reads `refunded_at`. Fine for a
  pilot, thin for accounts.
- **An owner cannot refund at a PLATFORM kiosk**, by design — see above. If
  that turns out to be wrong for the pilot, the change is a product decision
  about whose money a shop may hand back, not a loosened check.
- **The invoice has no address for the owner and no tax line.** An account
  holds a name and an email and nothing else, and nothing stores a GSTIN for
  either side. `InvoiceParty.lines` is the seam: the day an owner billing
  profile exists, the renderer already prints it and one line in the route
  changes. Until then this is a payment record rather than a tax invoice, and
  it does not claim otherwise.
- **The migration from `printit_legacy` is not started**, and the dump has not
  arrived.

---

## Blocked on the operator

- a server, and DNS for `api.printvendo.com`;
- a Brevo API key that is not the development one;
- production Razorpay keys **and** a webhook secret;
- Redis on that server;
- a fresh `pg_dump` of production, taken during a maintenance window.

---

## Environment

```bash
py -3.12 -m venv .venv               # NOT `python` — that is 3.13 here
.venv/Scripts/pip install -e ".[dev]"
```

Postgres 18 on 5432, role `printvendo`, databases `printvendo` and
`printvendo_test`. Alembic reads `DATABASE_URL` from the **environment**:

```bash
export DATABASE_URL=$(grep -E "^DATABASE_URL=" .env | cut -d= -f2- | tr -d '\r')
```

### Running it, and clicking through it

```bash
.venv/Scripts/python -m uvicorn app.main:create_app --factory --port 8000
cd ../printvendo-web   && npm run dev                   # port 3000
cd ../printvendo-owner && npm run dev                   # port 3002
cd ../printvendo-admin && python -m http.server 3003    # the admin console
```

All three ports are in `CORS_ORIGINS` in `.env.example`. Sign in to the console
with `admin@printvendo.in` / `AdminConsole123!`, or mint your own — see
`printvendo-admin/README.md`. `admin@printvendo.test` is still on the dev
database and **can never sign in** (the `EmailStr` TLD issue above).

`python -m app.cli seed` builds a whole world and prints the passwords.
`python -m app.cli provision-kiosk --name "X" --type platform` stands up a real
one and prints its enrolment code and the installer command to spend it.

**Only PLATFORM kiosks take wallet money** — deliberate, enforced in two
places, and what to use for testing without a card.

---

## The method, briefly

`CLAUDE.md` has this in full. The two parts most often skipped:

1. **Mutation-test anything that matters** — and check the mutation failed the
   test you *meant*. One this session failed nothing, which is how a useless
   test was found.
2. **Say what is not done.** The live example is above: `printvendo-owner`
   compiles and has never been used.

Three mechanisms fail the build if you add a route and do not think:

- `tests/authz/matrix.py` — who may call it;
- `tests/authz/test_matrix_enforced.py` — and it is *checked*, in one direction
  only (see *Start here*);
- `tests/ops/audit_matrix.py` — whether it leaves an audit trail.

Do not work around them.
