# Handoff

Read `CLAUDE.md` first — it carries the conventions and the state of play, and
it is loaded automatically. This file carries only what CLAUDE.md does not: the
exact point work stopped, and the traps that cost time.

**Last updated: 2026-08-22, at commit `454aa27`.**

Update this file at the end of a session. Delete anything that has become true
in CLAUDE.md — two documents describing the same thing is how they drift.

---

## Where things stand

```
1300 tests passing · 11 import contracts · ruff clean · 96 routes (29 admin)
```

Verify before trusting that line:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/lint-imports && .venv/Scripts/python -m ruff check .
```

**Done:** core, identity, kiosks, payments, billing, printing, orders, wallet,
ops, every api layer including admin, and the **device socket**.

**Next, in dependency order:** migration → agent → cutover.

---

## Start here

**The migration** from `printit_legacy`, which is restored locally from a
production dump. Everything a person or a machine can do now has a route; what
is missing is the data.

It is still blocked on three decisions listed at the end of
`docs/superpowers/specs/2026-08-15-legacy-data-audit.md` — the ownerless SOLD
kiosk, the ten case-duplicate accounts, and the test/duplicate kiosks. Those are
the operator's calls, not code. **Ask for them before writing the migration**,
because each one changes what the script does rather than merely how it logs.

Two things already lean on those answers:

- `identity.repository.find_by_email` returns a **list**, so case-duplicate
  accounts are visible in the admin console rather than silently hidden by a
  `scalar_one_or_none`.
- Every table carries `legacy_id`, nullable and indexed, so a number that looks
  wrong afterwards can be traced to the row it came from.

**If the operator is not available**, the honest alternatives in rough order of
value: wire a scheduler for `expire_stale_orders` and `purge_expired_files`
(both have no caller); give `ops.raise_alert` its first callers, since the admin
console is currently correct and empty; or send email for real, since every
invitation and password reset is presently a log line.

The device agent rewrite is real work and is *not* blocked — the Pi has to learn
`/v1/device/ws`, treat `{"type": "wake"}` as "ask now", and keep polling as the
fallback. Doing it before the migration is defensible.

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
