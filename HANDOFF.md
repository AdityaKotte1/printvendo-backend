# Handoff

Read `CLAUDE.md` first — it carries the conventions and the state of play, and
it is loaded automatically. This file carries only what CLAUDE.md does not: the
exact point work stopped, and the traps that cost time.

**Last updated: 2026-08-21, at commit `41520d5`.**

Update this file at the end of a session. Delete anything that has become true
in CLAUDE.md — two documents describing the same thing is how they drift.

---

## Where things stand

```
1102 tests passing · 11 import contracts · ruff clean · 61 routes
```

Verify before trusting that line:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

**Done:** core, identity, kiosks, payments (incl. the real Razorpay HTTP client,
refunds, webhook), billing, printing, orders, wallet, ops, student-api,
owner-api.

**Next, in dependency order:** admin-api → device-ws → migration → agent →
cutover.

---

## Start here

**`admin-api` (`/v1/admin/*`).** Nothing else is blocked on anything else.

One loop is currently half-built and that is the first thing to close:

- An owner can submit a payment-key change request
  (`POST /v1/owner/payment-config/change-request`, built and tested).
- `payments.configs.review_change` exists and is tested.
- **Nothing can call it.** There is no admin route, so an owner who needs to
  change where their money goes submits a request that no one can approve.

Also unread without an admin surface: the `ops` audit trail and `AdminAlert`
list, both built last session and currently written but never displayed.

The change-request proof file needs an authenticated admin download route.
It is stored under `StorageArea.PROOF` and must **never** be served as a static
URL — the old admin dashboard built `API_BASE + '/storage/...'`, which 404s
silently behind an `onerror` handler, so admins were approving these having
never seen the proof.

---

## Traps that cost time in this session

Each of these produced a green test that proved nothing, or an hour of
debugging. They are not hypothetical.

**Assert on the error message, not just the status.** Two price-band tests
passed against a working band while proving nothing: the request used
`price_bw_single` where the schema says `bw_single`, so the payload was empty
and the 400 was "give at least one price to set". A second draft then tripped a
different rule again. A 400 for the wrong reason must fail.

**Check the real function signature before writing the test.** `effective_prices`
returns a dict, not an object. `document_for_user` raises `NotFound` rather than
returning `None`. `Subscription` has `expires_at`, not `current_period_end`.
Guessing cost three round trips each time.

**There are no ORM relationships across modules.** `Order` has no `.kiosk` and
`OrderItem` has no `.document` — a relationship is an import of another
context's table, which the import contracts forbid. Use `orders.views`, which
resolves public ids in two batched queries.

**Querying autoflushes.** A test asserting "this has not been written yet"
changes the answer by asking. Test the property that matters instead — whether
it survives a rollback — with `db.begin_nested()`.

**`git checkout -- a b` fails atomically.** If one path is untracked, neither
file is restored, so a mutation you thought you reverted is still applied.
Prefer reverting mutations with the same script that applied them.

**A mutation that survives may be pointing at a real bug.** Deleting the
webhook's duplicate-refund lookup broke no test — because reaching the fallback
meant an INSERT the unique index refuses, and `refund()` caught that
`IntegrityError` from a bare `flush()`, poisoning the caller's whole
transaction. The fix was a savepoint. Do not simply delete a guard that a
mutation says is redundant; find out why it looked redundant.

---

## The method, briefly

`CLAUDE.md` has this in full. The two parts most often skipped:

1. **Mutation-test anything that matters.** After a security or money rule
   lands, deliberately break it, confirm the *intended* test fails, restore.
   Several "passing" checks in this build turned out to be inspecting an empty
   set. A guardrail never seen to fail is not known to work.
2. **Say what is not done.** Partial work is reported as partial.

Two mechanisms fail the build if you add a route and do not think:

- `tests/authz/matrix.py` — who may call it.
- `tests/ops/audit_matrix.py` — whether it leaves an audit trail, `AUDITED` or
  `EXEMPT` with a named reason.

Both caught real mistakes the first time they ran. Do not work around them.

---

## Environment

```bash
py -3.12 -m venv .venv               # NOT `python` — that is 3.13 here
.venv/Scripts/pip install -e ".[dev]"
```

Postgres 18 on 5432, role `printvendo`, databases `printvendo` and
`printvendo_test`. Tests need `printvendo_test` to exist and fail loudly rather
than skip.

Alembic reads `DATABASE_URL` from the **environment**, not from `.env`:

```bash
export DATABASE_URL=$(grep -E "^DATABASE_URL=" .env | cut -d= -f2-)
```

Migrations are hand-written when autogenerate would be wrong — it wanted to
drop-and-add for a column rename, which discards every existing row's value.

---

## Open decisions, not open questions

Blocked on the operator, not on code:

- **Redis** — not installed. Only needed for the device WebSocket hub so
  production can run more than one worker. The device API works by polling
  without it. Deferred to staging.
- **Three data decisions** before the migration can be written: the ownerless
  SOLD kiosk, the ten case-duplicate accounts, the test/duplicate kiosks. See
  the end of `docs/superpowers/specs/2026-08-15-legacy-data-audit.md`.

---

## Known gaps that are not modules

These are real and currently unowned. None blocks `admin-api`.

- **Nothing runs on a schedule.** `expire_stale_orders` and
  `purge_expired_files` have no caller. Unpaid orders hold reserved paper
  forever and `FILE_RETENTION_DAYS = 7` is a promise nothing keeps.
- **Email is logged, not sent.** `LoggingNotifier` is wired; `BREVO_API_KEY` is
  read by nothing. In prod the app logger sits at WARNING, so password reset is
  effectively inert.
- **No rate limiting.** `slowapi` is a dependency and is wired to nothing.
- **`/health` never touches the database.**
- **The Dockerfile claims a Redis registry that does not exist**, and runs
  `--workers 4` on that basis. It happens to work because devices poll.
- **No `deploy/`** — no compose file, proxy config, backups or cron.

---

## Frontends

Not counted in the module list, and larger than the backend remainder in hours.

Backend is `api.printvendo.com` — **deliberately on the apps' apex.** The
refresh token is an httpOnly cookie with `SameSite=Lax`; a different
registrable domain would make every app→API call cross-site, the cookie would
be withheld on refresh, and every user would be signed out after 15 minutes.
Do not move the API to a separate apex without changing the cookie strategy
first.

| App | Domain | State |
|---|---|---|
| student | `printvendo.com` | exists, targets the legacy API |
| owner | `owner.printvendo.com` | unfinished, targets the legacy API |
| admin | `admin.printvendo.com` | does not exist |
| refiller | `refiller.printvendo.com` | exists; 3 endpoints to rewire |

**Pointing them at the new backend is a rewrite, not a config change.** Both
Next.js apps call the legacy contract — `/wallet/hold`, `/wallet/hold/multi`,
`/jobs/summary`, `/printers/`, `/payments/verify`. The two-call hold is exactly
the hazard the `Order` aggregate was built to make unreachable, so those routes
will never exist here. Each app's `lib/api.ts` gets rewritten.

Refiller is the cheap one: `/refiller/printers`, `.../paper/reset`,
`.../refill-logs` map almost one-to-one onto `/v1/refiller/kiosks/*`. Its real
work is the new auth flow and opaque `ksk_` ids replacing numeric ones.
