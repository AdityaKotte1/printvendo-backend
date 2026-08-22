# Handoff

Read `CLAUDE.md` first — it carries the conventions and the state of play, and
it is loaded automatically. This file carries only what CLAUDE.md does not: the
exact point work stopped, and the traps that cost time.

**Last updated: 2026-08-22, at commit `ba3b162`.**

Update this file at the end of a session. Delete anything that has become true
in CLAUDE.md — two documents describing the same thing is how they drift.

---

## Where things stand

```
1304 tests passing · 11 import contracts · ruff clean · 96 routes (29 admin)
```

Verify before trusting that line:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

**Done:** core, identity, kiosks, payments, billing, printing, orders, wallet,
ops, every api layer including admin, and the **device socket**.

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

All unblocked, in rough order of value: the **agent rewrite** (the Pi must learn
`/v1/device/ws`, treat `{"type": "wake"}` as "ask now", and keep polling as the
fallback); a **scheduler** for `expire_stale_orders` and `purge_expired_files`,
which have no caller; **the first callers for `ops.raise_alert`**, since the
admin console is correct and empty; or **sending email for real**, since every
invitation and password reset is currently a log line.

---

## Traps that cost time in this session

**`pathlib.read_text()` defaults to cp1252 on this machine.** Editing a file
that contains box-drawing characters — `matrix.py`, `audit_matrix.py`, most
module docstrings — silently fails to match, and a naive round-trip risks
mangling them. Always pass `encoding="utf-8"` to both `read_text` and
`write_text`.

**A `+` in a query string is a space.** `client.get(f"...?since={iso}")` with an
offset-aware datetime produced a 422, and the test failed on a `KeyError` in the
response body rather than on anything real. Pass `params={...}` and let the
client encode it.

**Bash heredocs choke on some of this prose.** Writing a test file with `cat
<<'EOF'` failed with "unexpected EOF"; the Write tool is the reliable path for
anything with apostrophes and unicode in it.

**A test can pass before the route exists.** `test_the_queue_never_carries_a
_storage_path` asserted `"proofs/" not in body` and passed against a 404. It
only became a real check once the route was written. Assert something the empty
case cannot satisfy, or run it once with the implementation in place.

**A surviving mutation may mean the test is aimed at the wrong property.**
Removing `revoke_all` from account deactivation broke nothing, because
`rotate_refresh` already refuses an inactive user — so a refresh attempted while
the account is off fails either way. The property the revocation is actually
load-bearing for is that a token taken *before* deactivation is still dead
*after* reactivation. That test now exists and was confirmed to fail without the
revocation.

**Check the shape a route returns before asserting on it.** Two billing tests
read `body["on_trial"]` where the response nests it under `subscription`. Same
class of mistake as last session's `effective_prices` returns-a-dict.

**A test can be undisprovable rather than merely weak.** A socket test
published one kiosk's wake and expected another's message; it could not fail,
because every wake reads `{"type": "wake"}` and the wrong one is
indistinguishable from the right one. Assert on what the code *chose* -- which
channel it subscribed to -- not on which of two identical messages arrived.

**Two harnesses collect routes, and they disagree about shape.**
`tests/authz` now yields WebSocket routes under the pseudo-method `WS`;
`tests/ops` imports the same `_flatten` and asks for `.methods`, which a
WebSocket route does not have. If you add another route kind, check both.

**Check that a database has tables, not just that it exists.** `printit_legacy`
connects happily and answers queries; it simply has nothing in it. "The dump is
restored" was true when the audit was written and had quietly stopped being
true. `select count(*) from information_schema.tables where
table_schema='public'` is the check that would have caught it.

**A new paper tray is full, not empty.** Paper is stored as sheets *used*, so
`used = 0` is a fresh ream. A test asserting `sheets_remaining == 0` on a new
kiosk is asserting the opposite of the intended behaviour.

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

- **Nothing runs on a schedule.** `expire_stale_orders` and
  `purge_expired_files` have no caller. Unpaid orders hold reserved paper
  forever and `FILE_RETENTION_DAYS = 7` is a promise nothing keeps.
- **Email is logged, not sent.** `LoggingNotifier` is wired; `BREVO_API_KEY` is
  read by nothing. In prod the app logger sits at WARNING, so password reset is
  effectively inert — and now so is every staff and owner invitation, including
  the one that assigns a kiosk to the shop that bought it.
- **Nothing raises an alert yet.** `ops.raise_alert` has an admin surface to be
  read from and no caller: no kiosk-offline detector, no low-paper watcher, no
  unsettled-payment sweep. The console will be correct and empty.
- **A subscription cannot be bought.** An admin can grant a trial and set terms,
  and `quote_subscription` prices a renewal, but there is no purchase route —
  `WebhookSettlement.settle_subscription` logs an error precisely because
  nothing can reach it. Owners are currently on trials or nothing.
- **No rate limiting.** `slowapi` is a dependency and is wired to nothing.
- **`/health` never touches the database.**
- **Redis is now genuinely required in production**, not merely claimed by the
  Dockerfile: `--workers 4` is correct only because the wake goes through
  pub/sub. Without Redis the socket degrades to nothing and devices poll, which
  is the pre-socket behaviour and still correct.
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
