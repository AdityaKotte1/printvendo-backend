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

## Status

Foundation and identity. **207 tests passing**, 5 import contracts kept.

- `app/core/` — config, db, ids, money, errors, crypto, security, notifier
- `app/modules/identity/` — users, roles, sessions, password/Google/guest
  sign-in, email verification
- `app/api/` — `deps` (current_user, role guards) and `student/auth`
  (`/v1/app/auth/*`, 9 routes)

Next is sub-project 3, kiosks — registry, `kiosk_type`, onboarding, pricing,
paper, assignments, and the refiller invite flow. It is the first module with
per-scope authorisation, so it is where `kiosk_scope` gets built.
