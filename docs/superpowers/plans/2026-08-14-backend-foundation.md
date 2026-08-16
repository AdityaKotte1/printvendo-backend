# PrintVendo Backend — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the new `printvendo-backend/` service — bootable, tested, containerised — with the structural guardrails (module boundaries, authorisation matrix, Postgres-only tests, Alembic) that the rest of the rewrite depends on.

**Architecture:** Modular monolith. `app/core/` holds primitives every module needs (config, db, ids, money, errors, security). `app/modules/` will hold bounded contexts, `app/api/` the thin per-audience route layers — both empty after this plan, but their boundaries are already enforced by `import-linter` in CI, so they cannot rot the way the old backend did.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres 16, Redis 7, pydantic-settings, passlib[bcrypt], python-jose, cryptography (Fernet), pytest, import-linter, ruff.

**Spec:** `docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md`

---

## Prerequisites (verified on this machine, 2026-08-14)

| Need | Status |
|---|---|
| Python 3.12 | Available as `py -V:3.12`. **The default `python` is 3.13 — always create the venv with `py -3.12`.** |
| Postgres | **Postgres 18 already runs natively** as Windows service `postgresql-x64-18` on port **5432**. `psql.exe` lives in `C:\Program Files\PostgreSQL\18\bin\`. No Docker needed. |
| Role + databases | Created by the operator: role `printvendo` / password `printvendo`, databases `printvendo` and `printvendo_test`. Dev-only credential; staging and production read `DATABASE_URL` from the environment. |
| Redis | **Not installed, and not needed by this plan.** Redis is first required by the device WebSocket hub in sub-project 4; installing it is that plan's problem. |
| Docker | **Not installed.** Only affects Task 11, which writes the production image — it is built on the VPS, not here. |

## Ground rules for this plan

**Production is never touched.** Everything here happens in a new directory
`printvendo-backend/` at the repo root, with its own git repo and its own
database. The existing `cloud-backend/` keeps running.

**Tests run against real Postgres. No SQLite.** The old backend used SQLite for
dev and Postgres in production, which lets dialect-specific bugs through. The dev
stack in Task 11 provides Postgres and Redis; unit tests that need neither run
without them.

**All money is `Decimal` rupees.** `float` never touches a monetary value.

---

## File structure

| Path | Responsibility |
|---|---|
| `printvendo-backend/pyproject.toml` | deps, pytest/ruff config |
| `printvendo-backend/.env.example` | every config key, no real values |
| `printvendo-backend/.importlinter` | module boundary contracts |
| `printvendo-backend/alembic.ini` | migration config |
| `printvendo-backend/app/core/money.py` | Decimal rupee helpers |
| `printvendo-backend/app/core/ids.py` | opaque prefixed public ids |
| `printvendo-backend/app/core/config.py` | settings + boot-time secret validation |
| `printvendo-backend/app/core/errors.py` | `{"detail": ...}` envelope + handlers |
| `printvendo-backend/app/core/db.py` | engine, session, declarative `Base` |
| `printvendo-backend/app/core/crypto.py` | envelope encryption for stored secrets |
| `printvendo-backend/app/core/security.py` | password hashing, JWT encode/decode |
| `printvendo-backend/app/main.py` | app factory, CORS, health |
| `printvendo-backend/migrations/` | Alembic environment + versions |
| `printvendo-backend/tests/authz/matrix.py` | route → allowed-role table |
| `printvendo-backend/Dockerfile` | production image (built on the VPS, not locally) |
| `printvendo-backend/CLAUDE.md` | component docs |

---

### Task 1: Project skeleton

**Files:**
- Create: `printvendo-backend/pyproject.toml`
- Create: `printvendo-backend/.gitignore`
- Create: `printvendo-backend/app/__init__.py`
- Create: `printvendo-backend/app/core/__init__.py`
- Create: `printvendo-backend/app/modules/__init__.py`
- Create: `printvendo-backend/app/api/__init__.py`
- Test: `printvendo-backend/tests/test_smoke.py`

- [ ] **Step 1: Create the directory and git repo**

```bash
cd "C:/Users/gurua/Downloads/Telegram Desktop/printit-upgrade"
mkdir -p printvendo-backend/app/core printvendo-backend/app/modules printvendo-backend/app/api printvendo-backend/tests
cd printvendo-backend
git init
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "printvendo-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy>=2.0.36",
    "psycopg[binary]>=3.2",
    "alembic>=1.14",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7.4",
    "bcrypt==4.0.1",
    "redis>=5.2",
    "cryptography>=43.0",
    "slowapi>=0.1.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-cov>=6.0",
    "httpx>=0.28",
    "import-linter>=2.1",
    "ruff>=0.8",
]

[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

`bcrypt` is pinned to 4.0.1 because passlib 1.7.4 reads `bcrypt.__about__.__version__`, which bcrypt 4.1+ removed. Unpinned, every password hash logs a spurious error.

- [ ] **Step 3: Write `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
.env
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
storage/
```

`*.egg-info/` matters: `pip install -e` generates it at the repo root and it
would otherwise churn into every commit.

- [ ] **Step 4: Create empty package files**

```bash
touch app/__init__.py app/core/__init__.py app/modules/__init__.py app/api/__init__.py tests/__init__.py
```

- [ ] **Step 5: Write the smoke test**

Create `tests/test_smoke.py`:

```python
def test_app_package_imports():
    import app

    assert app is not None
```

- [ ] **Step 6: Create the venv on Python 3.12 and install**

The default `python` on this machine is 3.13. Use the launcher to pin 3.12:

```bash
py -3.12 -m venv .venv
.venv/Scripts/python --version
```

Expected: `Python 3.12.x`. If it says 3.13, stop — the venv is wrong.

```bash
.venv/Scripts/pip install -e ".[dev]"
```

- [ ] **Step 7: Check whether the database is reachable (informational)**

```bash
"/c/Program Files/PostgreSQL/18/bin/psql.exe" \
  "postgresql://printvendo:printvendo@localhost:5432/printvendo_test" -c "select 1"
```

The operator creates role `printvendo` and databases `printvendo` /
`printvendo_test` in parallel with this work. **A failure here does not block
Tasks 1–7** — none of them touch a database. Report the result and carry on.
Task 8 is where it becomes a hard requirement.

- [ ] **Step 8: Run the smoke test**

```bash
.venv/Scripts/pytest tests/test_smoke.py -v
```

Expected: `1 passed`

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: scaffold printvendo-backend project"
```

---

### Task 2: Money primitives

Rupees, `Decimal`, two places, half-up. `float` is never valid for money.

**Files:**
- Create: `printvendo-backend/app/core/money.py`
- Test: `printvendo-backend/tests/core/test_money.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/__init__.py` (empty) and `tests/core/test_money.py`:

```python
from decimal import Decimal

import pytest

from app.core.money import as_money, from_paise, sum_money, to_paise


def test_as_money_quantizes_to_two_places():
    assert as_money(Decimal("1.005")) == Decimal("1.01")
    assert as_money(Decimal("1.004")) == Decimal("1.00")


def test_as_money_rounds_half_up_not_bankers():
    # Decimal's default is ROUND_HALF_EVEN, which would give 2.02 here.
    assert as_money(Decimal("2.025")) == Decimal("2.03")


def test_as_money_accepts_int_and_str():
    assert as_money(5) == Decimal("5.00")
    assert as_money("5.5") == Decimal("5.50")


def test_as_money_rejects_float():
    with pytest.raises(TypeError):
        as_money(1.1)


def test_to_paise():
    assert to_paise(Decimal("12.34")) == 1234
    assert to_paise(Decimal("0.05")) == 5


def test_from_paise():
    assert from_paise(1234) == Decimal("12.34")


def test_paise_roundtrip():
    amount = Decimal("199.99")
    assert from_paise(to_paise(amount)) == amount


def test_sum_money_of_empty_is_zero():
    assert sum_money([]) == Decimal("0.00")


def test_sum_money_quantizes_result():
    assert sum_money([Decimal("1.005"), Decimal("1.005")]) == Decimal("2.01")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/core/test_money.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.money'`

- [ ] **Step 3: Implement**

Create `app/core/money.py`:

```python
"""Money is rupees, Decimal, two places, ROUND_HALF_UP.

float is never a valid monetary type here. as_money rejects it outright rather
than silently accepting a value that has already lost precision.
"""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


def as_money(value: Decimal | int | str) -> Decimal:
    """Coerce to a two-place Decimal, rounding half up."""
    if isinstance(value, float):
        raise TypeError("float is not a valid money value; use Decimal or str")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_paise(value: Decimal) -> int:
    """Rupees to integer paise, for the Razorpay API."""
    return int(as_money(value) * 100)


def from_paise(paise: int) -> Decimal:
    """Integer paise back to rupees."""
    return as_money(Decimal(paise) / 100)


def sum_money(values: Iterable[Decimal]) -> Decimal:
    """Sum, quantized once at the end."""
    total = Decimal("0")
    for value in values:
        total += value
    return as_money(total)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/core/test_money.py -v`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add app/core/money.py tests/core/
git commit -m "feat(core): Decimal rupee money primitives"
```

---

### Task 3: Opaque public ids

Spec §4: every id crossing the API is an opaque prefixed string. Numeric primary
keys never leave the database.

**Files:**
- Create: `printvendo-backend/app/core/ids.py`
- Test: `printvendo-backend/tests/core/test_ids.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_ids.py`:

```python
import pytest

from app.core.ids import IdPrefix, InvalidId, new_id, parse_id


def test_new_id_has_prefix_and_separator():
    value = new_id(IdPrefix.KIOSK)
    assert value.startswith("ksk_")


def test_new_id_body_is_16_chars():
    value = new_id(IdPrefix.ORDER)
    assert len(value.split("_", 1)[1]) == 16


def test_new_id_avoids_ambiguous_characters():
    # Crockford base32 drops i, l, o and u so ids can be read aloud to support.
    for _ in range(200):
        body = new_id(IdPrefix.DOCUMENT).split("_", 1)[1]
        assert not set(body) & {"i", "l", "o", "u"}


def test_new_ids_are_unique():
    values = {new_id(IdPrefix.ORDER) for _ in range(1000)}
    assert len(values) == 1000


def test_parse_id_returns_body_for_matching_prefix():
    value = new_id(IdPrefix.KIOSK)
    assert parse_id(value, IdPrefix.KIOSK) == value.split("_", 1)[1]


def test_parse_id_rejects_wrong_prefix():
    value = new_id(IdPrefix.KIOSK)
    with pytest.raises(InvalidId):
        parse_id(value, IdPrefix.ORDER)


def test_parse_id_rejects_garbage():
    for bad in ["", "ksk", "ksk_", "_abc", "ksk_ABC!", "ksk_" + "a" * 17]:
        with pytest.raises(InvalidId):
            parse_id(bad, IdPrefix.KIOSK)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/core/test_ids.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.ids'`

- [ ] **Step 3: Implement**

Create `app/core/ids.py`:

```python
"""Opaque, prefixed public identifiers.

The old backend exposed both a numeric primary key and a public string for the
same printer, and callers passed the wrong one. Here the database key is never
serialised: every id crossing the API is `<prefix>_<16 chars>`, and parse_id
refuses an id of the wrong kind, so a document id can never be accepted where a
kiosk id belongs.

The alphabet is Crockford base32 minus i/l/o/u, so ids survive being read aloud.
"""

import secrets
from enum import StrEnum

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
BODY_LENGTH = 16


class IdPrefix(StrEnum):
    USER = "usr"
    KIOSK = "ksk"
    DEVICE = "dev"
    DOCUMENT = "doc"
    ORDER = "ord"
    PRINT_TASK = "tsk"
    PAYMENT = "pay"
    WALLET_ENTRY = "wlt"
    SUBSCRIPTION = "sub"
    ALERT = "alr"


class InvalidId(ValueError):
    """Raised when an id is malformed or of the wrong kind."""


def new_id(prefix: IdPrefix) -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LENGTH))
    return f"{prefix.value}_{body}"


def parse_id(value: str, expected: IdPrefix) -> str:
    """Return the body of `value`, or raise InvalidId."""
    if not isinstance(value, str) or "_" not in value:
        raise InvalidId(f"Malformed identifier: {value!r}")

    prefix, _, body = value.partition("_")
    if prefix != expected.value:
        raise InvalidId(f"Expected a {expected.value}_ identifier, got {prefix!r}")
    if len(body) != BODY_LENGTH or not set(body) <= set(ALPHABET):
        raise InvalidId(f"Malformed identifier body: {value!r}")
    return body
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/core/test_ids.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add app/core/ids.py tests/core/test_ids.py
git commit -m "feat(core): opaque prefixed public identifiers"
```

---

### Task 4: Settings and boot-time validation

The app must refuse to boot with a weak JWT secret or a missing webhook secret —
the old backend does this and it is worth keeping. CORS comes from env, never
hardcoded (spec §7).

**Files:**
- Create: `printvendo-backend/app/core/config.py`
- Create: `printvendo-backend/.env.example`
- Test: `printvendo-backend/tests/core/test_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE_ENV = {
    "ENV": "dev",
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/pv",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET_KEY": "x" * 32,
    "SECRETS_ENCRYPTION_KEY": "k" * 44,
    "RAZORPAY_KEY_ID": "rzp_test_abc",
    "RAZORPAY_KEY_SECRET": "shh",
    "RAZORPAY_WEBHOOK_SECRET": "hook",
    "CORS_ORIGINS": "http://localhost:3000,http://localhost:3002",
}


def test_settings_load_from_env():
    settings = Settings(**BASE_ENV)
    assert settings.ENV == "dev"
    assert settings.DATABASE_URL.startswith("postgresql+psycopg://")


def test_cors_origins_parsed_into_list():
    settings = Settings(**BASE_ENV)
    assert settings.cors_origins == [
        "http://localhost:3000",
        "http://localhost:3002",
    ]


def test_cors_origins_strips_whitespace_and_blanks():
    settings = Settings(**{**BASE_ENV, "CORS_ORIGINS": " http://a.test , ,http://b.test "})
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_short_jwt_secret_is_rejected():
    with pytest.raises(ValidationError):
        Settings(**{**BASE_ENV, "JWT_SECRET_KEY": "tooshort"})


def test_prod_requires_webhook_secret():
    env = {**BASE_ENV, "ENV": "prod", "RAZORPAY_WEBHOOK_SECRET": ""}
    with pytest.raises(ValidationError):
        Settings(**env)


def test_dev_tolerates_missing_webhook_secret():
    settings = Settings(**{**BASE_ENV, "RAZORPAY_WEBHOOK_SECRET": ""})
    assert settings.RAZORPAY_WEBHOOK_SECRET == ""


def test_prod_rejects_wildcard_cors():
    env = {**BASE_ENV, "ENV": "prod", "CORS_ORIGINS": "*"}
    with pytest.raises(ValidationError):
        Settings(**env)


def test_access_token_lifetime_defaults_to_15_minutes():
    assert Settings(**BASE_ENV).ACCESS_TOKEN_MINUTES == 15
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/core/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.config'`

- [ ] **Step 3: Implement**

Create `app/core/config.py`:

```python
"""Application settings.

Two rules the old backend learned the hard way and this keeps:
  * the app refuses to boot with a weak JWT secret or, in production, without a
    Razorpay webhook secret;
  * the CORS allowlist comes from the environment, never from source, so adding
    a frontend is a deploy variable rather than a code change.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENV: Literal["dev", "staging", "prod"] = "dev"

    DATABASE_URL: str
    REDIS_URL: str

    JWT_SECRET_KEY: str = Field(min_length=32)
    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 30

    # Fernet key (urlsafe base64, 44 chars) used to encrypt stored third-party
    # secrets such as an owner's Razorpay key secret. See app/core/crypto.py.
    SECRETS_ENCRYPTION_KEY: str = Field(min_length=44)

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    GOOGLE_CLIENT_ID: str = ""
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = ""
    BREVO_API_KEY: str = ""

    GHOSTSCRIPT_PATH: str = "gs"
    STORAGE_ROOT: str = "./storage"

    CORS_ORIGINS: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> "Settings":
        if self.ENV != "prod":
            return self
        if not self.RAZORPAY_WEBHOOK_SECRET:
            raise ValueError("RAZORPAY_WEBHOOK_SECRET is required in production")
        if "*" in self.cors_origins:
            raise ValueError("Wildcard CORS origin is not allowed in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Write `.env.example`**

```dotenv
ENV=dev

DATABASE_URL=postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo
# Redis is not used until sub-project 4 (device WebSocket hub). The value is
# validated as present but nothing connects to it yet.
REDIS_URL=redis://localhost:6379/0

# 32+ chars. Generate: python -c "import secrets;print(secrets.token_urlsafe(48))"
JWT_SECRET_KEY=
ACCESS_TOKEN_MINUTES=15
REFRESH_TOKEN_DAYS=30

# Generate: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
SECRETS_ENCRYPTION_KEY=

RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=

GOOGLE_CLIENT_ID=
VAPID_PUBLIC_KEY=
VAPID_PRIVATE_KEY=
VAPID_SUBJECT=
BREVO_API_KEY=

GHOSTSCRIPT_PATH=gs
STORAGE_ROOT=./storage

CORS_ORIGINS=http://localhost:3000,http://localhost:3002
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/core/test_config.py -v`
Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py .env.example tests/core/test_config.py
git commit -m "feat(core): settings with boot-time secret and CORS validation"
```

---

### Task 5: Error envelope

Spec §7: errors keep `{"detail": "<human sentence>"}`. `printvendo-owner`
surfaces `detail` verbatim to the user, so the shape and the humanity of the
message are both part of the contract.

**Files:**
- Create: `printvendo-backend/app/core/errors.py`
- Test: `printvendo-backend/tests/core/test_errors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_errors.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    install_error_handlers,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/notfound")
    def _notfound():
        raise NotFound("Kiosk not found")

    @app.get("/forbidden")
    def _forbidden():
        raise Forbidden("You do not have access to this kiosk")

    @app.get("/conflict")
    def _conflict():
        raise Conflict("That refiller is already assigned")

    @app.get("/boom")
    def _boom():
        raise RuntimeError("internal detail that must not leak")

    return TestClient(app, raise_server_exceptions=False)


def test_not_found_returns_404_with_detail(client):
    response = client.get("/notfound")
    assert response.status_code == 404
    assert response.json() == {"detail": "Kiosk not found"}


def test_forbidden_returns_403_with_detail(client):
    response = client.get("/forbidden")
    assert response.status_code == 403
    assert response.json() == {"detail": "You do not have access to this kiosk"}


def test_conflict_returns_409_with_detail(client):
    response = client.get("/conflict")
    assert response.status_code == 409


def test_unexpected_error_returns_500_without_leaking_internals(client):
    response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong. Please try again."}
    assert "internal detail" not in response.text


def test_app_error_is_the_common_base():
    assert issubclass(NotFound, AppError)
    assert issubclass(Forbidden, AppError)
    assert issubclass(Conflict, AppError)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/core/test_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.errors'`

- [ ] **Step 3: Implement**

Create `app/core/errors.py`:

```python
"""One error envelope: {"detail": "<sentence a human can act on>"}.

printvendo-owner renders `detail` straight to the user, so these strings are
product copy, not debug output. Unexpected exceptions never reach the client —
they are logged and replaced with a generic sentence, because a stack trace or
an ORM message is an information leak.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base for every error that is safe to show a user."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class BadRequest(AppError):
    status_code = 400


class Unauthorized(AppError):
    status_code = 401


class Forbidden(AppError):
    status_code = 403


class NotFound(AppError):
    status_code = 404


class Conflict(AppError):
    status_code = 409


class TooManyRequests(AppError):
    status_code = 429


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong. Please try again."},
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/core/test_errors.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add app/core/errors.py tests/core/test_errors.py
git commit -m "feat(core): single error envelope with no internal leakage"
```

---

### Task 6: Secret encryption at rest

Spec §6, Finding 1: owner Razorpay secrets are plaintext in the current
production database. This is the primitive that fixes it, and the migration
depends on it.

**Files:**
- Create: `printvendo-backend/app/core/crypto.py`
- Test: `printvendo-backend/tests/core/test_crypto.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_crypto.py`:

```python
import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SecretBox, mask_secret

KEY = Fernet.generate_key().decode()


def test_roundtrip():
    box = SecretBox(KEY)
    assert box.decrypt(box.encrypt("rzp_live_supersecret")) == "rzp_live_supersecret"


def test_ciphertext_is_not_the_plaintext():
    box = SecretBox(KEY)
    assert "supersecret" not in box.encrypt("rzp_live_supersecret")


def test_same_plaintext_encrypts_differently_each_time():
    box = SecretBox(KEY)
    assert box.encrypt("same") != box.encrypt("same")


def test_wrong_key_cannot_decrypt():
    ciphertext = SecretBox(KEY).encrypt("secret")
    other = SecretBox(Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        other.decrypt(ciphertext)


def test_tampered_ciphertext_is_rejected():
    box = SecretBox(KEY)
    ciphertext = box.encrypt("secret")
    tampered = ciphertext[:-2] + ("aa" if not ciphertext.endswith("aa") else "bb")
    with pytest.raises(ValueError):
        box.decrypt(tampered)


def test_tampering_the_middle_of_the_token_is_rejected():
    """The tail-swap above can fail base64 decoding rather than HMAC checking.

    Tampering in the middle lands inside the ciphertext body, so this is the
    test that actually proves authentication rather than parsing.
    """
    box = SecretBox(KEY)
    ciphertext = box.encrypt("a secret long enough to have a middle")
    middle = len(ciphertext) // 2
    swapped = "b" if ciphertext[middle] != "b" else "c"
    tampered = ciphertext[:middle] + swapped + ciphertext[middle + 1 :]

    assert tampered != ciphertext
    with pytest.raises(ValueError):
        box.decrypt(tampered)


def test_mask_shows_only_the_last_four_characters():
    assert mask_secret("rzp_live_abcd1234") == "••••1234"


def test_mask_of_short_value_reveals_nothing():
    assert mask_secret("abc") == "••••"


def test_mask_of_empty_value_is_empty():
    assert mask_secret("") == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/core/test_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.crypto'`

- [ ] **Step 3: Implement**

Create `app/core/crypto.py`:

```python
"""Encryption for third-party secrets held on behalf of someone else.

An owner's Razorpay key secret is stored encrypted, never in plaintext, so a
database dump does not hand over live payment credentials. Fernet gives
authenticated encryption, so a tampered ciphertext fails instead of decrypting
to garbage.

Nothing here is for passwords. Passwords are hashed, not encrypted — see
app/core/security.py.
"""

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Could not decrypt secret: wrong key or tampered data") from exc


def mask_secret(value: str) -> str:
    """What an API is allowed to return about a stored secret."""
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••{value[-4:]}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/core/test_crypto.py -v`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add app/core/crypto.py tests/core/test_crypto.py
git commit -m "feat(core): envelope encryption for stored third-party secrets"
```

---

### Task 7: Password hashing and JWTs

**Files:**
- Create: `printvendo-backend/app/core/security.py`
- Test: `printvendo-backend/tests/core/test_security.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_security.py`:

```python
from datetime import timedelta

import pytest

from app.core.security import (
    TokenError,
    TokenType,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)

SECRET = "s" * 32


def test_hash_is_not_the_password():
    assert hash_password("hunter2") != "hunter2"


def test_hash_is_salted():
    assert hash_password("hunter2") != hash_password("hunter2")


def test_verify_accepts_correct_password():
    assert verify_password("hunter2", hash_password("hunter2")) is True


def test_verify_rejects_wrong_password():
    assert verify_password("wrong", hash_password("hunter2")) is False


def test_access_token_roundtrip():
    token = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(minutes=15))
    claims = decode_token(token, TokenType.ACCESS, SECRET)
    assert claims.subject == "usr_abc"


def test_refresh_token_carries_a_family_id():
    token = create_token(
        "usr_abc", TokenType.REFRESH, SECRET, timedelta(days=30), family="fam_1"
    )
    assert decode_token(token, TokenType.REFRESH, SECRET).family == "fam_1"


def test_refresh_token_is_rejected_where_an_access_token_is_required():
    token = create_token("usr_abc", TokenType.REFRESH, SECRET, timedelta(days=30))
    with pytest.raises(TokenError):
        decode_token(token, TokenType.ACCESS, SECRET)


def test_expired_token_is_rejected():
    token = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(seconds=-1))
    with pytest.raises(TokenError):
        decode_token(token, TokenType.ACCESS, SECRET)


def test_token_signed_with_another_secret_is_rejected():
    token = create_token("usr_abc", TokenType.ACCESS, "o" * 32, timedelta(minutes=15))
    with pytest.raises(TokenError):
        decode_token(token, TokenType.ACCESS, SECRET)


def test_every_token_has_a_unique_jti():
    a = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(minutes=15))
    b = create_token("usr_abc", TokenType.ACCESS, SECRET, timedelta(minutes=15))
    assert decode_token(a, TokenType.ACCESS, SECRET).jti != decode_token(
        b, TokenType.ACCESS, SECRET
    ).jti
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/core/test_security.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.security'`

- [ ] **Step 3: Implement**

Create `app/core/security.py`:

```python
"""Password hashing and JWT issue/verify.

Every token carries an explicit `typ` claim and decode_token requires the
expected one, so a long-lived refresh token can never be replayed where a
15-minute access token is required. Every token also carries a unique `jti`,
which is what makes refresh rotation with reuse detection possible in the
identity module.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from jose import JWTError, jwt
from passlib.context import CryptContext

ALGORITHM = "HS256"

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """Token missing, malformed, expired, mistyped or badly signed."""


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    token_type: TokenType
    jti: str
    family: str | None


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd.verify(password, hashed)
    except ValueError:
        return False


def create_token(
    subject: str,
    token_type: TokenType,
    secret: str,
    lifetime: timedelta,
    family: str | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": subject,
        "typ": token_type.value,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    if family is not None:
        payload["fam"] = family
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, expected: TokenType, secret: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise TokenError(str(exc)) from exc

    if payload.get("typ") != expected.value:
        raise TokenError(f"Expected a {expected.value} token")

    return TokenClaims(
        subject=payload["sub"],
        token_type=expected,
        jti=payload["jti"],
        family=payload.get("fam"),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/core/test_security.py -v`
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add app/core/security.py tests/core/test_security.py
git commit -m "feat(core): password hashing and typed JWTs"
```

---

### Task 8: Database session and declarative base

> **Hard requirement:** role `printvendo` and database `printvendo_test` must
> exist on the local Postgres 18. Verify with the psql command from Task 1 Step 7
> before starting. If it fails, report NEEDS_CONTEXT rather than working around
> it — no SQLite fallback.

**Files:**
- Create: `printvendo-backend/app/core/db.py`
- Test: `printvendo-backend/tests/core/test_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_db.py`:

```python
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from app.core.db import Base, get_engine, session_scope


@pytest.fixture
def probe_table(postgres_url: str) -> Iterator[str]:
    """A scratch table, created and dropped around each test that needs one.

    A fixture rather than an ordered pair of tests: relying on an earlier test
    to have created the table makes the suite order-dependent, and a "cleanup
    test" that asserts nothing is not a test.
    """
    with session_scope(postgres_url) as session:
        session.execute(text("drop table if exists scope_probe"))
        session.execute(text("create table scope_probe (id int primary key)"))
    try:
        yield "scope_probe"
    finally:
        with session_scope(postgres_url) as session:
            session.execute(text("drop table if exists scope_probe"))


def _row_count(url: str) -> int:
    with session_scope(url) as session:
        return session.execute(text("select count(*) from scope_probe")).scalar_one()


def test_base_uses_a_shared_metadata():
    assert Base.metadata is not None


def test_engine_is_cached_per_url():
    url = "postgresql+psycopg://u:p@localhost:5432/pv"
    assert get_engine(url) is get_engine(url)


def test_session_scope_yields_a_working_session(postgres_url):
    with session_scope(postgres_url) as session:
        assert session.execute(text("select 1")).scalar_one() == 1


def test_session_scope_commits_on_success(postgres_url, probe_table):
    with session_scope(postgres_url) as session:
        session.execute(text("insert into scope_probe values (1)"))

    assert _row_count(postgres_url) == 1


def test_work_before_an_exception_is_discarded(postgres_url, probe_table):
    """The contract: an exception escaping the block leaves nothing behind.

    Note this holds whether the discard comes from the explicit rollback() or
    from close() tearing down the transaction — both are in session_scope, and
    the behaviour is what callers depend on. Do not read a passing test here as
    licence to delete the explicit rollback(); it states intent at the point
    where the decision is made.
    """
    with session_scope(postgres_url) as session:
        session.execute(text("insert into scope_probe values (1)"))

    with pytest.raises(RuntimeError):
        with session_scope(postgres_url) as session:
            session.execute(text("insert into scope_probe values (2)"))
            raise RuntimeError("caller blew up after writing")

    assert _row_count(postgres_url) == 1


def test_the_exception_propagates_rather_than_being_swallowed(postgres_url, probe_table):
    with pytest.raises(RuntimeError, match="caller blew up"):
        with session_scope(postgres_url) as session:
            session.execute(text("insert into scope_probe values (3)"))
            raise RuntimeError("caller blew up")
```

**Verified during implementation:** deleting `session.rollback()` from
`session_scope` does **not** fail these tests, because `session.close()` tears
the transaction down anyway. The docstring says so explicitly, so nobody later
reads a green suite as permission to remove it. The tests assert the
*behavioural* contract — nothing survives an exception — which is what callers
actually depend on.

- [ ] **Step 2: Add the Postgres fixture**

Create `tests/conftest.py`:

```python
import os

import pytest
from sqlalchemy import create_engine, text

DEFAULT_TEST_URL = "postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo_test"


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """URL of a live Postgres for tests.

    Tests run against real Postgres, never SQLite: the old backend used SQLite
    in dev and Postgres in production, which let dialect-specific bugs through.
    Locally this is the machine's Postgres 18 service; CI overrides it with
    TEST_DATABASE_URL pointing at a service container.
    """
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Postgres is not reachable at {url}.\n{exc}")
    finally:
        engine.dispose()
    return url
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/core/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.db'`

- [ ] **Step 4: Implement**

Create `app/core/db.py`:

```python
"""Engine, session and the declarative base every module's tables hang off.

One Base and one metadata for the whole service: modules own their tables, but
they share a schema and a migration history.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


@lru_cache
def get_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    """A session that commits on success and rolls back on any exception."""
    session = get_session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 5: Run to verify it passes**

```bash
.venv/Scripts/pytest tests/core/test_db.py -v
```

Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add app/core/db.py tests/core/test_db.py tests/conftest.py
git commit -m "feat(core): engine, session scope and declarative base"
```

---

### Task 9: App factory and health endpoint

**Files:**
- Create: `printvendo-backend/app/main.py`
- Test: `printvendo-backend/tests/test_main.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_main.py`:

```python
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY="x" * 32,
    SECRETS_ENCRYPTION_KEY="k" * 44,
    CORS_ORIGINS="http://localhost:3000",
)


def test_health_returns_ok():
    client = TestClient(create_app(SETTINGS))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_version_and_env():
    client = TestClient(create_app(SETTINGS))
    body = client.get("/health").json()
    assert body["version"]
    assert body["env"] == "dev"


def test_cors_allows_a_configured_origin():
    client = TestClient(create_app(SETTINGS))
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_does_not_allow_an_unconfigured_origin():
    client = TestClient(create_app(SETTINGS))
    response = client.get("/health", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in response.headers


def test_error_handlers_are_installed():
    app = create_app(SETTINGS)
    from app.core.errors import AppError

    assert AppError in app.exception_handlers
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: Implement**

Create `app/main.py`:

```python
"""Application factory.

create_app takes Settings explicitly so tests can build an app without touching
the environment, and so a future worker process can build one with a different
configuration. The CORS allowlist comes from settings — adding a frontend is a
deploy variable, never a code change.

**There is deliberately no module-level `app = create_app()`.** Building the app
at import time would call get_settings(), so importing this module would require
a fully populated environment — which breaks pytest collection, Alembic and
import-linter, none of which have any business needing production config. Run it
with uvicorn's factory flag instead:

    uvicorn app.main:create_app --factory
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers

VERSION = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(title="PrintVendo API", version=VERSION)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_error_handlers(app)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION, "env": settings.ENV}

    return app
```

**Discovered during implementation:** a module-level `app = create_app()` makes
`import app.main` fail without a complete environment, which breaks pytest
collection. Hence the factory-only form above and `--factory` everywhere the app
is served.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/test_main.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: app factory with env-driven CORS and health endpoint"
```

---

### Task 10: Alembic

Spec §10: Alembic migrations run as a pre-start job, replacing the old
backend's 27 ad-hoc `migrate_*.py` scripts.

**Files:**
- Create: `printvendo-backend/alembic.ini`
- Create: `printvendo-backend/migrations/env.py`
- Create: `printvendo-backend/migrations/script.py.mako`
- Create: `printvendo-backend/migrations/versions/` (empty)
- Test: `printvendo-backend/tests/test_migrations.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migrations.py`:

```python
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _alembic(args: list[str], url: str) -> subprocess.CompletedProcess:
    # Inherit the real environment and override only DATABASE_URL. Replacing
    # os.environ wholesale breaks the subprocess on Windows, which needs PATH
    # and SYSTEMROOT to start Python at all.
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_upgrade_head_succeeds_on_an_empty_database(postgres_url):
    result = _alembic(["upgrade", "head"], postgres_url)
    assert result.returncode == 0, result.stderr


def test_autogenerate_detects_no_drift_after_upgrade(postgres_url):
    _alembic(["upgrade", "head"], postgres_url)
    result = _alembic(["check"], postgres_url)
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/test_migrations.py -v`
Expected: FAIL — alembic exits non-zero, no `alembic.ini`

- [ ] **Step 3: Initialise Alembic**

```bash
.venv/Scripts/alembic init migrations
```

- [ ] **Step 4: Configure `alembic.ini`**

Replace the `sqlalchemy.url` line with an empty value — the URL comes from the
environment in `env.py`:

```ini
sqlalchemy.url =
```

- [ ] **Step 5: Rewrite `migrations/env.py`**

```python
"""Alembic environment.

The database URL comes from the environment, never from alembic.ini, so the same
migration set runs against dev, test and production without editing a file.

target_metadata is app.core.db.Base.metadata, and every module's models are
imported below so autogenerate sees the whole schema. A module whose models are
not imported here is invisible to migrations.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError("DATABASE_URL must be set to run migrations")
config.set_main_option("sqlalchemy.url", database_url)

# Module models are imported here as they are built. Empty for now.
# from app.modules.identity import models as _identity_models  # noqa: F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 5b: Modernise `migrations/script.py.mako`**

Alembic's stock revision template emits `typing.Union` / `typing.Sequence`,
which ruff's `UP` rules reject — so **every new revision would arrive with lint
errors**, and lint that is always red gets ignored. Fix the template once:

Replace the import block and the four identifier lines with:

```mako
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}
```

Note there is **no** blank line between `${imports if imports else ""}` and the
comment: when `imports` is empty that line renders as a blank one, and an extra
blank on top of it trips ruff's `I001`.

Add to `pyproject.toml`, because a revision that adds no columns leaves `sa` and
`Sequence` genuinely unused:

```toml
[tool.ruff.lint.per-file-ignores]
"migrations/versions/*.py" = ["F401"]
```

Verify the template rather than trusting it: generate a throwaway revision, run
`ruff check .`, confirm it is clean, then delete the file.

- [ ] **Step 6: Create the baseline revision**

PowerShell:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo_test"
.venv\Scripts\alembic revision -m "baseline"
```

Git Bash:

```bash
DATABASE_URL="postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo_test" \
  .venv/Scripts/alembic revision -m "baseline"
```

Leave the generated `upgrade()` and `downgrade()` bodies as `pass`. This revision
exists so later migrations have a root; it creates nothing.

- [ ] **Step 7: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/test_migrations.py -v`
Expected: `2 passed`

- [ ] **Step 8: Commit**

```bash
git add alembic.ini migrations tests/test_migrations.py
git commit -m "feat: Alembic migrations driven by DATABASE_URL"
```

---

### Task 11: Production image

The dev stack was built in Task 1. This task adds the image the server runs.

**Files:**
- Create: `printvendo-backend/Dockerfile`
- Create: `printvendo-backend/.dockerignore`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY migrations ./migrations
COPY alembic.ini ./

EXPOSE 8000

# Workers > 1 is safe here: the device WebSocket registry lives in Redis, not in
# a per-process dict. This is the constraint the old backend could never lift.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers"]
```

`COPY app` must come **before** `pip install .`, not after. `pyproject.toml`
declares `[tool.setuptools.packages.find] include = ["app*"]`, so installing
with the package directory absent produces a wheel containing no packages —
the dependencies land but the app itself does not, and it only appears to work
afterwards because `WORKDIR /app` puts the later-copied source on `sys.path`.

- [ ] **Step 2: Write `.dockerignore`**

```gitignore
.venv/
venv/
__pycache__/
*.egg-info/
.pytest_cache/
.ruff_cache/
tests/
storage/
.env
.git/
.github/
```

- [ ] **Step 3: Verify what can be verified**

Docker is not installed on this machine, so the image cannot be built here. It
is built on the VPS at deploy time. Verify by inspection instead:

- every path the `COPY` lines reference exists (`app/`, `migrations/`, `alembic.ini`, `pyproject.toml`)
- `.dockerignore` excludes `.env`, `.git/`, `.venv/` and `tests/`
- the `CMD` runs `alembic upgrade head` before `uvicorn`

Building the image is a step in the deploy plan, not this one.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "chore: production image with migrations on start"
```

---

### Task 12: Module boundary enforcement

Spec §3: `import-linter` is what stops the architecture rotting the way the old
backend's did. Contracts are written now, while there is nothing to violate them,
so the first violation fails CI instead of becoming precedent.

**Files:**
- Create: `printvendo-backend/.importlinter`
- Test: `printvendo-backend/tests/test_architecture.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_architecture.py`:

```python
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_import_contracts_hold():
    # import-linter ships a `lint-imports` console script; there is no
    # `python -m importlinter` entry point, so resolve the script on PATH.
    executable = shutil.which("lint-imports")
    if executable is None:
        pytest.fail("lint-imports not found. Run `pip install -e \".[dev]\"` first.")

    result = subprocess.run(
        [executable],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/test_architecture.py -v`
Expected: FAIL — no `.importlinter` configuration file

- [ ] **Step 3: Write `.importlinter`**

```ini
[importlinter]
root_package = app

[importlinter:contract:core-is-independent]
name = core must not depend on modules or api
type = forbidden
source_modules =
    app.core
forbidden_modules =
    app.modules
    app.api

[importlinter:contract:modules-do-not-depend-on-api]
name = modules must not depend on the api layer
type = forbidden
source_modules =
    app.modules
forbidden_modules =
    app.api

[importlinter:contract:layers]
name = api sits above modules sits above core
type = layers
layers =
    app.api
    app.modules
    app.core
```

Two further contracts are added by later plans, once there is something to
constrain: `modules-are-independent` (an `independence` contract naming every
bounded context, so no module may import another's internals) and
`api-touches-no-orm` (forbidding `sqlalchemy` inside `app.api`).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/test_architecture.py -v`
Expected: `1 passed`

Confirm the linter is actually checking something:

```bash
.venv/Scripts/lint-imports
```

Expected: `Contracts: 3 kept, 0 broken.`

- [ ] **Step 5: Commit**

```bash
git add .importlinter tests/test_architecture.py
git commit -m "chore: enforce module boundaries with import-linter"
```

---

### Task 13: Authorisation matrix harness

Spec §9: the authorisation matrix is generated from the route table, so **a new
route with no matrix entry fails the build**. This is what makes "an owner
controls only their own kiosks" non-regressable. Building the harness now means
every route added from here on is forced through it.

**Files:**
- Create: `printvendo-backend/tests/authz/__init__.py`
- Create: `printvendo-backend/tests/authz/matrix.py`
- Create: `printvendo-backend/tests/authz/test_matrix_complete.py`

- [ ] **Step 1: Write the failing test**

Create `tests/authz/__init__.py` (empty) and `tests/authz/test_matrix_complete.py`:

```python
"""Every route must declare who may call it.

This test exists to fail. When someone adds a route and does not add it to
MATRIX, the build breaks and they are forced to state the authorisation rule
rather than inherit whatever the surrounding router happened to do. That is the
mechanism the old backend lacked, and why /owner/* ended up carrying a
"DO NOT LOOSEN" comment instead of a check.
"""

import pytest
from fastapi.routing import APIRoute

from app.core.config import Settings
from app.main import create_app
from tests.authz.matrix import KNOWN_AUDIENCES, MATRIX

IGNORED_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY="x" * 32,
    SECRETS_ENCRYPTION_KEY="k" * 44,
    CORS_ORIGINS="http://localhost:3000",
)


def declared_routes() -> list[tuple[str, str]]:
    app = create_app(SETTINGS)
    found = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in IGNORED_PATHS:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, route.path))
    return sorted(found)


def test_every_route_has_a_matrix_entry():
    missing = [route for route in declared_routes() if route not in MATRIX]
    assert not missing, (
        "These routes have no authorisation matrix entry. Add them to "
        f"tests/authz/matrix.py and state who may call them: {missing}"
    )


def test_matrix_has_no_entries_for_routes_that_no_longer_exist():
    routes = set(declared_routes())
    stale = [entry for entry in MATRIX if entry not in routes]
    assert not stale, f"Matrix entries for routes that do not exist: {stale}"


@pytest.mark.parametrize("route", declared_routes())
def test_matrix_entry_is_a_known_audience_set(route):
    allowed = MATRIX[route]
    assert allowed, f"{route} declares an empty audience set"
    assert allowed <= KNOWN_AUDIENCES, f"{route} names an unknown audience"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/pytest tests/authz -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.authz.matrix'`

- [ ] **Step 3: Implement the matrix**

Create `tests/authz/matrix.py`:

```python
"""Who may call each route.

One entry per (method, path). The audience set is exhaustive — anyone not named
must be refused. Later plans add, alongside this table, the test that actually
exercises each route as each audience against own/other/no scope; this file is
the single declaration those tests read.
"""

PUBLIC = "public"
STUDENT = "student"
OWNER = "owner"
REFILLER = "refiller"
ADMIN = "admin"
DEVICE = "device"

KNOWN_AUDIENCES = {PUBLIC, STUDENT, OWNER, REFILLER, ADMIN, DEVICE}

MATRIX: dict[tuple[str, str], set[str]] = {
    ("GET", "/health"): {PUBLIC},
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/pytest tests/authz -v`
Expected: `3 passed`

- [ ] **Step 5: Prove the harness actually bites**

Temporarily add a route to `app/main.py` inside `create_app`, above `return app`:

```python
    @app.get("/temp-check")
    def _temp_check() -> dict[str, str]:
        return {"ok": "yes"}
```

Run: `.venv/Scripts/pytest tests/authz -v`
Expected: FAIL — `These routes have no authorisation matrix entry ... [('GET', '/temp-check')]`

Now delete those four lines from `app/main.py` and re-run.
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add tests/authz/
git commit -m "test: authorisation matrix harness that fails on undeclared routes"
```

---

### Task 14: CI

**Files:**
- Create: `printvendo-backend/.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: printvendo
          POSTGRES_PASSWORD: printvendo
          POSTGRES_DB: printvendo_test
        ports:
          - 5433:5432
        options: >-
          --health-cmd "pg_isready -U printvendo"
          --health-interval 5s
          --health-retries 10

    env:
      TEST_DATABASE_URL: postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo_test

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install -e ".[dev]"

      - name: Lint
        run: ruff check .

      - name: Module boundaries
        run: lint-imports

      - name: Tests
        run: pytest -q
```

- [ ] **Step 2: Verify locally**

```bash
.venv/Scripts/ruff check .
.venv/Scripts/lint-imports
.venv/Scripts/pytest -q
```

Expected: ruff clean, `Contracts: 3 kept, 0 broken.`, all tests pass.

- [ ] **Step 3: Commit**

```bash
git add .github/
git commit -m "ci: lint, module boundaries and tests on Postgres"
```

---

### Task 15: Component documentation

**Files:**
- Create: `printvendo-backend/CLAUDE.md`
- Create: `printvendo-backend/README.md`
- Modify: `CLAUDE.md` (repo root component map)

- [ ] **Step 1: Write `printvendo-backend/CLAUDE.md`**

````markdown
# printvendo-backend

The rebuilt central API. Replaces `cloud-backend/`, which stays deployed and
serving production until cutover. See
`docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md` for the design
and the reasoning behind everything below.

## Stack

Python 3.12, FastAPI, SQLAlchemy 2.0, Postgres 16, Redis 7, Alembic.

## Commands

```bash
py -3.12 -m venv .venv               # NOT `python` — that is 3.13 here
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/uvicorn app.main:create_app --factory --reload --port 8000
.venv/Scripts/pytest -q
.venv/Scripts/lint-imports           # module boundary contracts
.venv/Scripts/ruff check .
```

Copy `.env.example` to `.env` and fill it. The app refuses to boot with a
`JWT_SECRET_KEY` under 32 characters, and in production without a
`RAZORPAY_WEBHOOK_SECRET`.

**Database.** Uses the machine's local Postgres 18 service on port 5432, role
`printvendo`, databases `printvendo` and `printvendo_test`. Tests require
`printvendo_test` to exist and will fail loudly rather than silently skip.
Redis appears in config but nothing connects to it until the device hub lands.

## Layout

- `app/core/` — primitives every module needs: `config`, `db`, `ids`, `money`,
  `errors`, `crypto`, `security`. Depends on nothing else in `app`.
- `app/modules/` — bounded contexts. A module owns its tables; no other module
  may import its ORM models, only its service functions.
- `app/api/` — thin per-audience route layers (`student`, `owner`, `admin`,
  `refiller`, `device`). Authenticate, validate, call one service, serialise.

## Conventions that are not preferences

- **Organise by subject, never by audience.** The old backend's routers were
  per-audience, so paper reset existed four times and clear-queue twice, and the
  copies drifted. Five audiences share one implementation of each subject.
- **`import-linter` enforces the above** (`.importlinter`, run in CI). A
  boundary violation fails the build rather than becoming precedent.
- **Every route needs an entry in `tests/authz/matrix.py`** or the build fails.
  Adding a route forces you to state who may call it.
- **Money is `Decimal` rupees**, two places, `ROUND_HALF_UP`. `app.core.money.as_money`
  raises on `float` rather than accepting a value that already lost precision.
- **Public ids are opaque and prefixed** (`ksk_…`, `ord_…`). Numeric primary keys
  never leave the database, so a caller can never pass the wrong kind of id.
- **Errors are `{"detail": "<human sentence>"}`.** `printvendo-owner` renders
  `detail` straight to the user; those strings are product copy.
- **Tests run on real Postgres, never SQLite.** The old backend's SQLite-in-dev
  split let dialect bugs through.
- **Third-party secrets are encrypted at rest** via `app.core.crypto.SecretBox`
  and returned only masked. The old backend stored owner Razorpay key secrets in
  plaintext.
- **Workers may exceed 1.** The device WebSocket registry lives in Redis, not a
  per-process dict.

## Status

Foundation only: core primitives, app factory, health, Alembic, dev stack, CI,
boundary and authorisation harnesses. No domain modules yet — see §12 of the
spec for the build order.
````

- [ ] **Step 2: Write `README.md`**

```markdown
# printvendo-backend

Rebuilt central API for PrintVendo. Not yet deployed — `cloud-backend/` serves
production.

Design: `../docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md`
Working notes and conventions: `CLAUDE.md`

## Quick start

```bash
cp .env.example .env      # then fill JWT_SECRET_KEY and SECRETS_ENCRYPTION_KEY
py -3.12 -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/pytest -q
.venv/Scripts/uvicorn app.main:create_app --factory --reload --port 8000
```

Requires a local Postgres with role `printvendo` and databases `printvendo`
and `printvendo_test`.

Health check: <http://localhost:8000/health>
```

- [ ] **Step 3: Add the component to the repo-root `CLAUDE.md` table**

In `C:/Users/gurua/Downloads/Telegram Desktop/printit-upgrade/CLAUDE.md`, add a
row to the component map immediately after the `cloud-backend/` row:

```markdown
| `printvendo-backend/` | FastAPI + SQLAlchemy 2.0 + Postgres + Redis | **Rebuilt** central API — replaces `cloud-backend/` at cutover | not yet deployed |
```

- [ ] **Step 4: Verify the full suite**

```bash
.venv/Scripts/pytest -q
.venv/Scripts/lint-imports
.venv/Scripts/ruff check .
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: component documentation for printvendo-backend"
```

The root `CLAUDE.md` edit is outside this git repo — the repo root is not a git
repository, so that change is saved but not committed.

---

## Outcome (completed 2026-08-14)

All 15 tasks done, 15 commits, **69 tests passing**, `lint-imports` 3 kept /
0 broken, `ruff` clean, working tree clean.

Six defects in this plan were found and fixed during implementation. Each is
corrected in the task text above; recorded here so the pattern is visible:

| # | Defect | Why it mattered |
|---|---|---|
| 1 | `.gitignore` missed `*.egg-info/` | `pip install -e` artifacts churned into the first commit |
| 2 | Dev stack was Task 11 but Task 8 needed Postgres | tasks could not run in order |
| 3 | `app = create_app()` at module level | importing `app.main` required full config, breaking pytest collection |
| 4 | Rollback test proved nothing | passed with `session.rollback()` deleted, since `close()` discards anyway |
| 5 | Alembic's stock template is not ruff-clean | every future revision would arrive with lint errors |
| 6 | Dockerfile ran `pip install .` before `COPY app` | produced a wheel with no packages |

Two guardrails were verified by deliberately breaking them, not by assuming:

- **import-linter**: adding `import app.modules` to `app/core/money.py` →
  exit 1, 2 contracts broken. Restored → exit 0, 3 kept. (Clear `__pycache__`
  between runs or `grimp` reads stale bytecode and reports the old result.)
- **authorisation matrix**: adding an undeclared `/temp-check` route → build
  fails naming the route and telling the developer to declare it.

## Done when

- `uvicorn app.main:create_app --factory` serves `GET /health` → `{"status":"ok",...}`
- `alembic upgrade head` runs clean against an empty database
- `pytest -q` passes end to end against real Postgres
- `lint-imports` reports `3 kept, 0 broken`
- Adding a route without a matrix entry **fails the build** (verified in Task 13)
- `cloud-backend/` is untouched and production is unaffected

## Next plan

Sub-project 2, **identity** — users, roles, sessions, and the login methods
(email, Google, guest), including refresh rotation with reuse detection built on
`app.core.security`. It is the first plan that adds real routes, so it is also
the first real exercise of the authorisation matrix.
