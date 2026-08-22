# Handoff

Read `CLAUDE.md` first — it carries the conventions and the state of play, and
it is loaded automatically. This file carries only what CLAUDE.md does not: the
exact point work stopped, and the traps that cost time.

**Last updated: 2026-08-22, at commit `677d95a`.**

Update this file at the end of a session. Delete anything that has become true
in CLAUDE.md — two documents describing the same thing is how they drift.

---

## Where things stand

```
1280 tests passing · 11 import contracts · ruff clean · 95 routes (29 admin)
```

Verify before trusting that line:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

**Done:** core, identity, kiosks, payments, billing, printing, orders, wallet,
ops, and the student, owner, refiller, device and **admin** api layers.

**Next, in dependency order:** device-ws → migration → agent → cutover.

---

## Start here

**`device-ws`.** Everything a person can do now has a route; what is missing is
the machine half, and then the data.

The device API works today by polling (`POST /v1/device/tasks/next`), so this is
about latency and about lifting the one-worker constraint, not about correctness.
The registry lives in Redis rather than a per-process dict — that is the whole
reason the Dockerfile can honestly say `--workers 4`, which it currently claims
on a registry that does not exist.

- Redis is **not installed locally**, and the operator confirmed that is a
  production concern rather than a local one. Build against `fakeredis`; real
  Redis is exercised at staging.
- The claim path is already atomic (`FOR UPDATE SKIP LOCKED`) and lease
  recovery already exists, so a socket that drops mid-job is a solved problem.
  Do not reimplement either behind the hub.

**Then the migration**, which is still blocked on three data decisions listed at
the end of `docs/superpowers/specs/2026-08-15-legacy-data-audit.md`: the
ownerless SOLD kiosk, the ten case-duplicate accounts, and the test/duplicate
kiosks. `identity.repository.find_by_email` returns a **list** specifically so
those duplicates are visible in the admin console rather than hidden.

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

**A new paper tray is full, not empty.** Paper is stored as sheets *used*, so
`used = 0` is a fresh ream. A test asserting `sheets_remaining == 0` on a new
kiosk is asserting the opposite of the intended behaviour.

---

## The method, briefly

`CLAUDE.md` has this in full. The two parts most often skipped:

1. **Mutation-test anything that matters.** After a security or money rule
   lands, deliberately break it, confirm the *intended* test fails, restore.
   Nine mutations this session; eight failed the right test, and the one that
   survived found a missing test rather than a redundant guard.
2. **Say what is not done.** Partial work is reported as partial.

Two mechanisms fail the build if you add a route and do not think:

- `tests/authz/matrix.py` — who may call it.
- `tests/ops/audit_matrix.py` — whether it leaves an audit trail, `AUDITED` or
  `EXEMPT` with a named reason.

Both fired on every admin router added this session. Do not work around them.

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

Real, currently unowned, and none of them blocks `device-ws`.

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
