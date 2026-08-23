# Handoff

Read `CLAUDE.md` first — it carries the conventions and the state of play, and
it is loaded automatically. This file carries only what CLAUDE.md does not: the
exact point work stopped, and the traps that cost time.

**Last updated: 2026-08-24, at commit `95b0165`.**

Update this file at the end of a session. Delete anything that has become true
in CLAUDE.md — two documents describing the same thing is how they drift.

---

## Where things stand

```
1552 tests passing · 12 import contracts · ruff clean · 105 routes (31 admin)
```

Verify before trusting that line:

```bash
.venv/Scripts/python -m pytest -q
PYTHONIOENCODING=utf-8 .venv/Scripts/lint-imports    # see the traps
.venv/Scripts/python -m ruff check .
```

**The backend is feature-complete for a pilot**, and the pilot now has a
machine to run on. Every audience has a working surface, money can be taken
*and given back*, an owner-collecting shop can pay to stay open, a kiosk can be
stood up in one command, and a Pi or a Windows PC can be enrolled against it in
one more.

Three repositories are current and talk to each other:

| Repo | State |
|---|---|
| `printvendo-backend` (here) | `95b0165` |
| `printvendo-agent` | `2baaae8` — new this session, replaces `pi-agent/` and `windows-agent (1)/` |
| `printvendo-web` | `bf0a9be` — the only frontend on this backend |

`printvendo-owner`, the admin console and the refiller app are all still on the
legacy API or do not exist.

---

## Start here

**Deploy.** It is the only thing between here and a shop taking real money, and
everything it needs from the operator is listed under *Blocked on the operator*
below. The shape:

- a `deploy/` that does not exist yet: compose, a proxy, TLS, backups;
- `docs_url=None` in production — `/docs`, `/redoc` and `/openapi.json`
  currently publish every admin route to anybody;
- `TRUST_PROXY_HEADERS` set, or every request appears to come from the proxy
  and the whole internet shares one rate-limit bucket;
- real Redis: the device socket needs it, and the rate limiter wants it past
  one worker. Both degrade rather than break without it, which is why local
  development has never had it and staging must.

Then, in the order agreed with the operator: **push notifications**, then the
**paper shop** (the operator has decided the *admin* manages the catalogue).
Then migration rehearsals and the consoles.

### Before deploy, if there is a spare hour

**Connect the wake socket in the agent.** `/v1/device/ws` is built, tested and
waiting; the agent polls every fifteen seconds instead. Correct, and slower than
it needs to be — a student watches that fifteen seconds.

---

## What was done this session

- **`printvendo-agent`** — one agent, both platforms, on the new contract.
  `pi-agent` already contained Windows code that filled in a `DEVMODE` and never
  applied it, then shelled out to the print verb, dropping every option;
  `windows-agent (1)` was a second implementation that claimed one task per pass,
  which is the "only the first of four files prints" bug. Both spoke the legacy
  API. Neither was fixed; both stay in the repo until cutover and must not be
  edited.
- **Queued, printing, printed — read off the printer.** `lp` returns when a job
  is *queued* and Ghostscript returns when the *spooler* has the data, so
  neither is a state worth reporting. The queue is polled instead: CUPS by
  position in `lpstat -o`, Windows by the spooler's own status bits. Verified
  live on an eighty-page job.
- **An order follows its prints.** `DISPATCHED`, `COMPLETED` and
  `PARTIALLY_FAILED` existed and nothing set them — a student's screen said
  "queued" while the paper was in their hand. `refresh_order_state`, reached
  through the `TaskOutcome` seam, which `start_printing` now uses too.
- **Installers that cannot print say so, at install** — see the traps.
- **`SETUP.md`** in the agent repo: the whole procedure, including the two steps
  neither installer can do (provisioning the kiosk for a code, and adding a
  printer to CUPS on a headless Pi).
- `provision-kiosk` now prints the two installer commands rather than a curl.

---

## Traps that cost time in this session

**`"$PSScriptRoot[windows]"` in PowerShell is an index into the path string**,
not a path with a pip extras suffix. It installed nothing and said nothing.
`"$($PSScriptRoot)[windows]"`.

**The Windows agent runs as SYSTEM, and SYSTEM has its own printer list.** A
printer added under one user account — which is what happens with most network
printers — is invisible to it. An install could finish successfully on a machine
that would claim jobs and never print them. The installer now enumerates
printers *as SYSTEM*, through a one-off scheduled task, so the list it checks is
the list the service will have.

**`python` on a fresh Windows PATH is the Microsoft Store stub.** It looks like
Python until the venv fails.

**The Windows spooler names every Ghostscript job "Ghostscript output"**,
whatever the file. Matching a job by document name matched nothing, and the
whole wait became a no-op that looked exactly like a working one. Match on job
**ids snapshotted before printing**. Found by watching a real queue, not by
reading the docs.

**Writing a long document through a bash heredoc in this tool fails on
apostrophes** — twice, with `unexpected EOF while looking for matching '`. Use
the Write tool for prose. Heredocs are still fine for code and commit messages.

### Still true from last session

**`next build` while `next dev` is running breaks the dev server.** Shared
`.next`; every route starts 500ing with `MODULE_NOT_FOUND`. Stop dev,
`rm -rf .next`, start again.

**Do not run two pytest sessions against the same Postgres.** A foreground run
started while a background full run was going produced `DeadlockDetected` in a
test that was fine.

**`tests/test_migrations.py` leaves the schema wherever the migration under test
left it.** Fixed via `tests/conftest.rebuild_schema`, which imports every model
first — a *partial* `Base.metadata` fails on a foreign key to a table nobody
imported.

**Autogenerated foreign keys are anonymous, and the downgrade cannot drop what
it cannot name.** Name the constraint in both directions.

**`git checkout <file>` is not how you undo a mutation test.** It reverts your
own uncommitted work in that file, and does nothing at all for an untracked one.
Copy the file aside instead.

**`lint-imports > /dev/null` exits 1 whatever the contracts say** — redirecting
stdout puts `rich` into legacy-Windows rendering, which cannot encode its banner
in cp1252. Use `PYTHONIOENCODING=utf-8`.

**import-linter: `|` means independent siblings, `:` does not.**

**A `\n` inside a bash heredoc that writes Python becomes a real newline.**
Build such strings with `chr(10)`/`chr(92)`, or write separate `print()` calls.

**A test can pass before the route exists.** Assert the status code first.

**`b"text" in pdf` is false for a PDF that plainly says so** — reportlab
compresses content streams. Read it back with pypdf.

---

## Known gaps that are not modules

- **No `deploy/`** — see *Start here*.
- **Rate limits are per address, not per account.** A campus shares one NAT, so
  they stop a script rather than credential stuffing aimed at one person.
- **No push notifications** — VAPID keys are read by nothing.
- **No paper-shop catalogue** — `ItemKind.SHOP_ITEM` exists and nothing else
  mentions it.
- **No owner-facing refund**, and no owner console: `printvendo-owner` is
  unfinished and still on the legacy API. The admin console does not exist;
  `/docs` plus the seeded admin is the admin surface today.
- **Nothing sweeps for unsettled payments** — the third watcher the alerts table
  was built for.
- **The agent does not connect the wake socket**, and reports `sheets_printed`
  from the server's own figure rather than the printer's count (deliberate: one
  calculation decides price, tray and what the printer is asked for).
- **The migration from `printit_legacy` is not started**, and the dump has not
  arrived.

---

## Blocked on the operator

Everything deploy needs and nothing here can invent:

- a server, and DNS for `api.printvendo.com`;
- a Brevo API key that is not the development one;
- production Razorpay keys **and** a webhook secret — the app refuses to boot in
  production without `RAZORPAY_WEBHOOK_SECRET`;
- Redis on that server;
- a fresh `pg_dump` of production, taken during a maintenance window. The local
  `printit_legacy` restore is **gone**; the legacy *schema* comes from
  `cloud-backend/app/models/` and every figure in the data audit is illustrative.

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
cd ../printvendo-web && npm run dev          # port 3000, in .env already
```

`python -m app.cli seed` builds a whole world and prints the passwords.
`python -m app.cli provision-kiosk --name "X" --type platform` stands up a real
one and prints its enrolment code and the installer command to spend it;
`--type sold --owner-email …` invites an owner and says what it is waiting for.

The dev database has seeded shops. **Only PLATFORM kiosks take wallet money** —
that is deliberate and enforced in two places, so use one for testing without a
card.

### Testing against real hardware

Bind uvicorn to `0.0.0.0` and give the kiosk machines the laptop's LAN address
as `--api`. `printvendo-agent/SETUP.md` has the whole procedure, including
adding a printer to CUPS over SSH and what each failure means. Plain HTTP is
fine on a bench and not in a shop: a device token crosses that wire on every
request.

---

## The method, briefly

`CLAUDE.md` has this in full. The two parts most often skipped:

1. **Mutation-test anything that matters.** This session: the `TaskOutcome`
   notification in `start_printing` — removed, and it failed exactly the test
   that asserts it and nothing else.
2. **Say what is not done.** Partial work is reported as partial.

Three mechanisms fail the build if you add a route and do not think:

- `tests/authz/matrix.py` — who may call it;
- `tests/authz/test_matrix_enforced.py` — and it is *checked*, by firing every
  audience the matrix does not name at every route;
- `tests/ops/audit_matrix.py` — whether it leaves an audit trail.

Do not work around them.
