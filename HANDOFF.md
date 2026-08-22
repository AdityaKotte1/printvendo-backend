# Handoff

Read `CLAUDE.md` first — it carries the conventions and the state of play, and
it is loaded automatically. This file carries only what CLAUDE.md does not: the
exact point work stopped, and the traps that cost time.

**Last updated: 2026-08-22, at commit `02107ab`.**

Update this file at the end of a session. Delete anything that has become true
in CLAUDE.md — two documents describing the same thing is how they drift.

---

## Where things stand

```
1406 tests passing · 12 import contracts · ruff clean · 97 routes (29 admin)
```

Verify before trusting that line:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

**Done:** core, identity, kiosks, payments, billing, printing, orders, wallet,
ops, every api layer including admin, the **device socket**, and — this session
— **Phase 1, the backend's last wiring**: rate limits, a `/health` that asks the
database, the scheduler and its four sweeps, the first `raise_alert` callers,
and the owner orders CSV.

**Next, in dependency order:** migration → agent → cutover. The migration's
three blocking decisions are answered; it is waiting on a production dump, not
on a decision.

---

## Start here

**The migration reader**, written against the legacy *schema* and tested on
synthetic data. The three decisions that used to block it are answered; what is
missing now is the production rows, and they arrive later.

### What changed, and why it matters

**The local `printit_legacy` restore is gone.** Verified 2026-08-22: that
database exists, as do `printhub`, `printvending` and `smartprint`, and every one
of them has **zero tables**. Do not go looking for it, and do not trust any
figure in the data audit — kiosk 35, the ten duplicate pairs, the ~4,000
abandoned checkouts are all illustrative now.

**The operator will hand over a fresh dump** taken while production is in
maintenance. Ask for `pg_dump` with schema *and* data, so the plan step can
refuse a column-type mismatch loudly instead of coercing it.

**So the schema comes from `cloud-backend/app/models/`** — the running
production backend's own SQLAlchemy models. That is authoritative and closer to
production than a dump would be. Build the reader against those and test the
whole migration on a synthetic legacy database created from them inside
`printvendo_test`. When the dump lands, the same code points at a real database.

### The rules, already decided

Recorded in full at the end of
`docs/superpowers/specs/2026-08-15-legacy-data-audit.md`. In short:

- **Kiosks are created through `create_kiosk`, never copied**, and climb the
  onboarding ladder — so the payment gate still stands between a SOLD kiosk and
  LIVE. Each records `legacy_id` (`ba3b162`), which is how orders and payments
  find their kiosk. **The run fails if any legacy kiosk that took a payment is
  unmapped**; a silently smaller revenue total is the one outcome this must not
  have.
- **An ownerless SOLD kiosk is created as PLATFORM** and reported.
- **Case-colliding accounts merge onto the oldest**, balances summed, every
  merge itemised.
- **Test and duplicate kiosks are not a list in code.** The plan step generates
  the candidates from the fresh dump for the operator to confirm; anything that
  took a real payment is imported whatever it is called.

### Shape

**Plan, then apply.** Read, print what would be created, mapped, merged and
quarantined with row counts and wallet/revenue totals, get approval, then apply
in one transaction and refuse to finish if the totals disagree.

### If you would rather not start there

The phase plan agreed with the operator runs: **1 backend finishing** (done),
**2 deploy**, **3 Pi agent**, **4 student app**, **5 migration rehearsal**,
**6 cutover**, **7 the remaining consoles**. Phases 2 and 5 need something from
the operator — server access, DNS, a Brevo key, production Razorpay keys, and a
fresh dump — so the unblocked work is:

- the **agent rewrite** (the Pi must learn `/v1/device/ws`, treat
  `{"type": "wake"}` as "ask now", and keep polling as the fallback);
- the **migration reader** above, on synthetic data;
- the **student app's `lib/api.ts`**, which still calls the legacy contract —
  see Frontends below and
  `docs/superpowers/specs/2026-08-22-student-app-api-gap.md`, which maps every
  call the app makes onto this backend and lists what has no home on either
  side. Its first item is small and overdue: the app has no
  `/verify-email`, `/reset-password` or `/accept-invite` page, and since
  `38c15be` those emails are really being sent.

---

## Traps that cost time in this session

**`git checkout <file>` is not how you undo a mutation.** Reverting `app/main.py`
after a mutation test also reverted the wiring that had been added to it minutes
earlier, and the file it was meant to restore — `app/api/ratelimit.py` — was
untracked, so the mutation stayed in place while the real work was thrown away.
Copy the file aside, or edit it back.

**A command whose exit code you are reading may be failing for another reason.**
`lint-imports > /dev/null` exits 1 on this machine whatever the contracts say:
redirecting stdout puts `rich` into legacy-Windows rendering, which cannot
encode its own banner in cp1252. Two "the contract caught it" results were that
crash. `PYTHONIOENCODING=utf-8` fixes it, and `tests/test_architecture.py`
already knew — it passes an encoding for exactly this reason.

**import-linter: `|` means independent, `:` does not.** `app.jobs : app.api`
looked like it declared two composition roots that may not import each other,
and allowed a job to import the api layer. Only the mutation test found it.

**Two pytest sessions against one Postgres deadlock.** A foreground run started
while a background full run was going produced `DeadlockDetected` in a test that
was fine. Wait for the background one.

**A test can pass before the route exists — again.** Four of the CSV export
tests passed against a 404, including "the student's email is not in the body".
They assert the status code first now. This is the second session in a row this
has happened; assume any new API test is vacuous until you have seen it fail.

**Adding a database read to `/health` broke a test that had nothing to do with
health.** Several test modules build `Settings` with a `DATABASE_URL` pointing
at a database that does not exist, which was harmless while `/health` only
looked at the framework.

**A paper counter that reads full is a sweep that reports nothing.** Paper is
stored as sheets *used*, so a `KioskPaper` row with `used = 0` is a full tray.
Test fixtures for the low-paper watcher have to set `used = capacity - wanted`.

---

## The method, briefly

`CLAUDE.md` has this in full. The two parts most often skipped:

1. **Mutation-test anything that matters.** After a security or money rule
   lands, deliberately break it, confirm the *intended* test fails, restore.
   Twelve mutations this session. Ten failed the intended test; one found a
   missing test rather than a redundant guard, and one found a test of mine
   that could not have failed.
2. **Say what is not done.** Partial work is reported as partial.

Two mechanisms fail the build if you add a route and do not think:

- `tests/authz/matrix.py` — who may call it.
- `tests/ops/audit_matrix.py` — whether it leaves an audit trail, `AUDITED` or
  `EXEMPT` with a named reason.

Both fired on every router added this session, and `tests/authz` had to be
taught to see WebSocket routes before it could fire on the last one. Do not work
around them.

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

Migrations are hand-written when autogenerate would be wrong. The most recent
one (`9a1c4d77e2b1`, the change-request public id) adds a column nullable,
backfills row by row — each id must be distinct, so one UPDATE with one
generated value would violate the unique index it is about to get — and only
then sets NOT NULL.

---

## Known gaps that are not modules

Real, currently unowned, and none of them blocks the migration.

- **A subscription cannot be bought.** An admin can grant a trial and set terms,
  and `quote_subscription` prices a renewal, but there is no purchase route —
  `WebhookSettlement.settle_subscription` logs an error precisely because
  nothing can reach it. Owners are currently on trials or nothing.
- **Rate limits are per address, not per account.** Enough to stop a script,
  not enough to stop credential stuffing aimed at one person, because a campus
  shares one NAT and the tight limit that would work locks out a lecture hall.
  Doing it properly needs the email out of the request body, which is a route's
  business rather than the edge's.
- **Nothing sweeps for unsettled payments.** Two of the three watchers the
  alerts table was built for exist (offline kiosks, paper); money that was
  taken and never settled is the third and has no detector.
- **Redis is genuinely required in production**, not merely claimed by the
  Dockerfile: `--workers 4` is correct for the wake because it goes through
  pub/sub, and correct for the rate limiter because its counts do too. Without
  Redis the socket degrades to polling and the limiter counts per process,
  which enforces four times the configured number — silently. `ENV != dev`
  chooses Redis, so there is no way to configure that mistake.
- **`TRUST_PROXY_HEADERS` must be set at deploy.** Behind Caddy or nginx and
  left false, every request arrives from the proxy and the whole internet
  shares one rate-limit bucket. Set true with no proxy in front and anyone can
  mint a fresh bucket per request.
- **No `deploy/`** — no compose file, proxy config, backups or cron. Cron
  matters less than it did: the sweeps run in the app.

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
| admin | `admin.printvendo.com` | does not exist — now has 29 routes waiting |
| refiller | `refiller.printvendo.com` | exists; 3 endpoints to rewire |

**Pointing them at the new backend is a rewrite, not a config change.** Both
Next.js apps call the legacy contract — `/wallet/hold`, `/wallet/hold/multi`,
`/jobs/summary`, `/printers/`, `/payments/verify`. The two-call hold is exactly
the hazard the `Order` aggregate was built to make unreachable, so those routes
will never exist here. Each app's `lib/api.ts` gets rewritten.

Refiller is the cheap one: `/refiller/printers`, `.../paper/reset`,
`.../refill-logs` map almost one-to-one onto `/v1/refiller/kiosks/*`. Its real
work is the new auth flow and opaque `ksk_` ids replacing numeric ones.
