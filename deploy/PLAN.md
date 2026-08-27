# Deploying printvendo-backend

**Status: a plan, not a run.** Nothing in this file has been executed against a
server. Every command is written to be copied, but none has been verified on
real infrastructure — treat the first run as the test of this document, and
correct it as you go.

The order matters. Each phase leaves something you can check before the next
one can break it.

---

## 0. What is actually being deployed

One FastAPI app, plus four background sweeps that run **inside** it.

- **Web**: `uvicorn app.main:create_app --factory`. No module-level `app`, so
  every process manager must use the factory form.
- **Sweeps**: order expiry, file retention, the offline-kiosk watcher, the
  paper watcher. They start in the app's `lifespan`, so **every worker runs the
  scheduler** and a `pg_try_advisory_lock` per job decides who actually does
  it. That is deliberate — you do **not** need a separate worker container, and
  adding one would run the sweeps twice.
- **Ghostscript**: the PDF pipeline shells out to it under `-dSAFER`. It must
  be installed in the image, or every upload fails at normalisation.
- **Storage**: `STORAGE_ROOT` holds uploaded documents and the bank-proof
  files. It is a **volume**, not a container layer — losing it loses every
  student's queued file and every proof an admin has yet to review.

Hard requirements: **Postgres 16+**, **Redis**, **Python 3.12**, Ghostscript,
and a reverse proxy terminating TLS.

---

## 1. Before you touch a server

These are the things that block a deploy and have nothing to do with a server.

### 1a. Close `/docs` in production — do this first

`app/main.py` builds `FastAPI(...)` with no `docs_url` argument, so
**`/docs`, `/redoc` and `/openapi.json` are public in every environment**,
publishing all 115 routes. It is a disclosure problem rather than a bypass —
every admin route is behind `require_role(ADMIN)` — but it is free to close:

```python
app = FastAPI(
    title="PrintVendo API",
    version=VERSION,
    lifespan=lifespan,
    # A schema is a map of the estate. Nothing behind it is unguarded, but
    # handing an attacker the list is a courtesy with no upside.
    docs_url=None if settings.ENV == "prod" else "/docs",
    redoc_url=None if settings.ENV == "prod" else "/redoc",
    openapi_url=None if settings.ENV == "prod" else "/openapi.json",
)
```

Write the test first — `tests/test_main.py` already builds apps from
`Settings`, so it is a three-line addition asserting `/docs` is 404 under
`ENV="prod"` and 200 under `ENV="dev"`.

### 1b. Generate the secrets

Never reuse the dev ones. Two are structured, not arbitrary:

```bash
# JWT_SECRET_KEY — the app refuses to boot under 32 characters
python -c "import secrets; print(secrets.token_urlsafe(48))"

# SECRETS_ENCRYPTION_KEY — must be a real Fernet key, validated at boot
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**`SECRETS_ENCRYPTION_KEY` is the one you cannot lose.** It decrypts every
owner's Razorpay key secret. Losing it does not lose money, but every
owner-collecting kiosk stops being able to take payment until each owner
re-enters their keys — and keys are set-once, so each one needs an admin
approval. Put it in a password manager before it goes on the server.

### 1c. Have these in hand

- A VPS. 2 vCPU / 4 GB is comfortable for a pilot; 1 GB is not, because
  Ghostscript and Postgres will fight.
- DNS **A** records pointing at it:
  `api.printvendo.com`, `printvendo.com`, `owner.printvendo.com`,
  `admin.printvendo.com`.
- **Production** Razorpay key id, key secret, **and webhook secret**. The app
  refuses to boot in `prod` without `RAZORPAY_WEBHOOK_SECRET`.
- A Brevo API key that is not the development one.

> **The API must stay on the apps' own apex.** The refresh token is an httpOnly
> cookie with `SameSite=Lax`. Put the API on a different registrable domain and
> the cookie is withheld on refresh, signing every user out a quarter of an hour
> after they sign in — the legacy "logs out frequently" bug by a new route.

---

## 2. The server

```bash
adduser --disabled-password --gecos "" printvendo
usermod -aG docker printvendo

ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable

# SSH: keys only
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh
```

Postgres and Redis bind to the Docker network only. **Neither is ever exposed
on a public port** — do not add a `ports:` mapping for them "just to check
something"; use `docker compose exec`.

---

## 3. Images

`Dockerfile` — note Ghostscript, and the non-root user:

```dockerfile
FROM python:3.12-slim

# Ghostscript is not optional: the PDF pipeline shells out to it under -dSAFER
# and every upload fails at normalisation without it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .

RUN useradd -r -u 10001 printvendo \
 && mkdir -p /var/lib/printvendo/storage \
 && chown -R printvendo /var/lib/printvendo
USER printvendo

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

**Workers may exceed 1** — the device WebSocket hub routes through Redis
pub/sub rather than a per-process dict, which is the constraint the old backend
could never lift. Four is a reasonable start on 2 vCPU.

`docker-compose.yml`:

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: printvendo
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: printvendo
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U printvendo"]
      interval: 10s
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "no", "--save", ""]
    restart: unless-stopped

  api:
    build: .
    env_file: [.env]
    volumes: [storage:/var/lib/printvendo/storage]
    depends_on:
      db: {condition: service_healthy}
    healthcheck:
      # /health runs `select 1`. A probe answering 200 from the framework alone
      # answers the one question nobody is asking.
      test: ["CMD-SHELL", "curl -fsS http://localhost:8000/health || exit 1"]
      interval: 15s
      start_period: 20s
    restart: unless-stopped

  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddydata:/data
    depends_on: [api]
    restart: unless-stopped

volumes: {pgdata: {}, storage: {}, caddydata: {}}
```

**Redis persistence is off on purpose.** It carries device wakes and rate-limit
counts. Both are derivable and both are allowed to be lost — polling is the
wake's fallback and the limiter fails open.

---

## 4. `.env` on the server

```bash
ENV=prod
DATABASE_URL=postgresql+psycopg://printvendo:${POSTGRES_PASSWORD}@db:5432/printvendo
REDIS_URL=redis://redis:6379/0

JWT_SECRET_KEY=<48 url-safe bytes>
SECRETS_ENCRYPTION_KEY=<a real Fernet key>

RAZORPAY_KEY_ID=rzp_live_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...        # boot fails in prod without it

# All four origins, explicitly. A wildcard refuses to boot in prod.
CORS_ORIGINS=https://printvendo.com,https://owner.printvendo.com,https://admin.printvendo.com

PUBLIC_BASE_URL=https://api.printvendo.com
APP_BASE_URL=https://printvendo.com

# Without this every request looks like it came from the proxy, so the whole
# internet shares one rate-limit bucket.
TRUST_PROXY_HEADERS=true

STORAGE_ROOT=/var/lib/printvendo/storage
GHOSTSCRIPT_PATH=/usr/bin/gs

BREVO_API_KEY=...
MAIL_FROM_EMAIL=hello@printvendo.com

INVOICE_ISSUER_NAME=Printvendo
INVOICE_ISSUER_LINES=Printvendo Technologies|<registered address>
INVOICE_ISSUER_EMAIL=billing@printvendo.com
```

`chmod 600 .env`. It holds the key that decrypts every owner's payment
credentials.

---

## 5. Proxy and TLS

`Caddyfile` — Caddy over nginx purely because it gets certificates itself:

```
api.printvendo.com {
	encode gzip
	# The device WebSocket needs no special handling in Caddy; it upgrades.
	reverse_proxy api:8000
	request_body {
		# Documents are uploaded here. Bigger than the largest PDF worth
		# printing, smaller than a denial of service.
		max_size 25MB
	}
}

printvendo.com        { root * /srv/web   ; file_server ; try_files {path} /index.html }
owner.printvendo.com  { root * /srv/owner ; file_server ; try_files {path} /index.html }
admin.printvendo.com  { root * /srv/admin ; file_server }
```

The three frontends are **static exports** — build them locally and copy `out/`
(or the three admin files) onto the server. `printvendo-admin`'s API origin
lives in `index.html` in **two adjacent lines that must agree**: the CSP
`connect-src` and `<meta name="printvendo-api">`. Changing one and not the
other produces an error naming the fallback directive rather than the mistake.

---

## 6. First boot

```bash
docker compose up -d db redis
docker compose run --rm api alembic upgrade head    # migrations, once
docker compose up -d

curl -fsS https://api.printvendo.com/health         # expect database: ok
curl -o /dev/null -w '%{http_code}\n' https://api.printvendo.com/docs   # expect 404
```

Migrations run as a **separate one-shot command**, never on app start: four
workers racing `alembic upgrade` is a schema nobody can predict.

Then mint the first admin — there is no other way in:

```bash
docker compose exec api python -m app.cli bootstrap-admin --email you@printvendo.com
```

> **`bootstrap-admin` accepts an address that cannot sign in.** It takes any
> string; `POST /v1/app/auth/login` validates with `EmailStr`, which rejects
> reserved TLDs like `.test`. Use a real address, or fix the CLI first.

---

## 7. Razorpay webhooks

Two shapes, and both must be registered.

1. **Platform**: `https://api.printvendo.com/v1/webhooks/razorpay`, signed with
   `RAZORPAY_WEBHOOK_SECRET`. Events: `payment.captured`, `payment.failed`,
   `refund.processed`.
2. **Per owner**: `https://api.printvendo.com/v1/webhooks/razorpay/{owner_id}`.
   Each owner registers this in **their own** dashboard and pastes their own
   signing secret into the owner app. The app hands them the exact URL, because
   a typo here is silent: deliveries arrive naming a different account, the
   signature check refuses them, and that owner's payments simply never settle.

Verify with Razorpay's "send test webhook" and watch `docker compose logs api`.
A subscription also settles from the browser, so a broken webhook will not
strand a subscription purchase — but it will strand print orders.

---

## 8. Backups — before real money, not after

```bash
# /etc/cron.daily/printvendo-backup
docker compose exec -T db pg_dump -U printvendo printvendo | zstd \
  > /backup/pv-$(date +%F).sql.zst
tar -C /var/lib/docker/volumes/deploy_storage/_data -czf /backup/storage-$(date +%F).tgz .
```

Ship both off the box — a backup on the server is not a backup. **Restore one
into a scratch database before you believe any of this**; an untested backup is
a folder of files.

Keep `SECRETS_ENCRYPTION_KEY` somewhere the backups are not. A dump without it
is inert; a dump beside it is every owner's payment credentials.

---

## 9. Cutover from `cloud-backend`

The legacy stack serves production today. It stays up until this replaces it.

1. **Staging first** — the whole of §1–8 against `staging.printvendo.com`, with
   Razorpay **test** keys. Click through all three consoles. Print one real job
   with a real agent.
2. **Freeze**: put the legacy backend into maintenance so no new orders land.
3. **Dump** production during the freeze — the migration reads a `pg_dump`
   taken in the window. The local `printit_legacy` restore is gone, so this
   dump is the only source of rows.
4. **Migrate**. It creates through the services rather than copying rows, so
   every invariant applies to imported data. Read
   `docs/superpowers/specs/2026-08-15-legacy-data-audit.md` first — the three
   data decisions are recorded there as rules.
5. **Verify**: kiosk count, owner count, a spot-check of balances, and that a
   SOLD kiosk with no keys is *not* LIVE.
6. **Switch DNS.** Keep the legacy stack running but unreferenced for a week.
7. **Agents**: each machine needs re-enrolling against the new API. Plan a
   window per shop — this is the slowest step and the one that touches people.

**Rollback** is DNS back to the legacy stack, which is why it stays up. Past the
point where students have placed orders on the new backend, rollback stops being
free — orders taken in between would have to be reconciled by hand.

---

## 10. What this plan does not cover

Said plainly rather than discovered later:

- **No log aggregation and no error tracking.** `docker compose logs` is the
  whole of observability. A Sentry DSN would be an hour's work and is worth it
  before real money.
- **No `/metrics`, no uptime monitor.** Point something external at `/health`
  at minimum, or you find out a shop is down from the shop.
- **Rate limits are per address, not per account.** Two hundred students behind
  one campus NAT share a bucket. They bound a script, not a person.
- **Nothing sweeps for unsettled payments** — the third watcher the alerts
  table was built for. A payment stuck between checkout and capture is
  currently invisible.
- **The storage volume has no size cap.** Retention sweeps completed jobs, but
  a disk-full is not handled gracefully anywhere.
- **This document is untested.** See the top.
