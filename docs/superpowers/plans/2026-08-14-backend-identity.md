# PrintVendo Backend — Identity Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `identity` bounded context — users, roles, sessions, and the four ways to sign in (email, Google, guest, refresh) — exposed at `/v1/app/auth/*`.

**Architecture:** A module under `app/modules/identity/` owning `users`, `user_roles` and `refresh_tokens`. It exposes typed service functions; nothing outside it touches its ORM models. The route layer at `app/api/student/auth.py` authenticates, validates, calls one service and serialises. This is the first sub-project to add real routes, so it is the first genuine exercise of the authorisation matrix.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres, `google-auth`, passlib, python-jose.

**Depends on:** `2026-08-14-backend-foundation.md` (complete — 74 tests, 16 commits)
**Spec:** `docs/superpowers/specs/2026-08-14-new-cloud-backend-design.md`

---

## Decisions carried in from reading the code being replaced

These are not stylistic. Each comes from behaviour in `cloud-backend/app/routers/auth.py`
that must be preserved or deliberately changed.

### D-I1 — Refresh rotation keeps a 60-second grace window

`auth.py:70` documents a real bug: rotating a refresh token and revoking the old
one *instantly* means two concurrent refreshes race. Two tabs, or one page load
firing several calls after the access token expires, and the loser is told its
token is invalid — so the client clears storage and bounces to `/login`. That
was the "logs out frequently" bug.

The spec calls for reuse detection (a replayed refresh revokes the whole
family). Implemented naively that **reintroduces the bug it just fixed**, more
aggressively — the losing tab would now kill every session the user has.

Resolution, and the rule this module implements:

| When the old token is presented | Interpretation | Action |
|---|---|---|
| Not revoked | normal rotation | rotate, issue new pair |
| Revoked **≤ 60s** ago | concurrent refresh from another tab | allow, return the replacement pair |
| Revoked **> 60s** ago | genuine replay of a stolen token | **revoke the entire family**, refuse |
| Expired | past the real deadline | refuse. Expiry is never graced. |

### D-I2 — Roles are ADMIN, OWNER, REFILLER, STUDENT. `is_guest` stays a column.

The spec's table lists five booleans collapsing into `UserRole`. Two of them are
not roles and are modelled accordingly:

- `is_guest` is an **account kind**, not a permission — a guest is a student who
  has not registered. It stays a column on `users`; guests still hold `STUDENT`.
- `subscription_enabled` is a **billing flag** and belongs to the billing module,
  not identity. It is not migrated into `user_roles`.

### D-I3 — Legacy password hashes

Already handled in `app/core/security.py` (commit `9d7f6c7`): `CryptContext`
accepts `pbkdf2_sha256` because that is what the old backend used for every
existing user, and `needs_rehash()` upgrades to bcrypt on next successful login.
**The login service must call it** — see Task 4.

### D-I4 — Behaviours preserved verbatim from the old auth router

- Refresh token is set as an **httpOnly, secure, samesite=lax cookie**, not
  returned in the JSON body.
- Guest accounts get a synthetic email `guest_<32 hex>@guest.printit`.
- Google tokens are verified **locally** with `google-auth`
  (`verify_oauth2_token`, `clock_skew_in_seconds=10`), never by calling a URL
  with the token in it.
- Google sign-in creates the user if the email is unknown.
- Rate limits: register 5/min, guest 5/min, google 10/min.

### D-I5 — The JWT subject is the public id, not the row id

The old backend put `str(user.id)` — the numeric primary key — in `sub`. Per
spec §4 the database key never leaves the database, so `sub` carries the opaque
`usr_…` public id.

---

## File structure

| Path | Responsibility |
|---|---|
| `app/modules/identity/__init__.py` | the module's public surface — re-exports services only |
| `app/modules/identity/models.py` | `User`, `UserRole`, `RefreshToken` ORM |
| `app/modules/identity/roles.py` | the `Role` enum |
| `app/modules/identity/repository.py` | all queries; no other module queries these tables |
| `app/modules/identity/passwords.py` | register / authenticate / rehash-on-login |
| `app/modules/identity/guests.py` | guest account creation |
| `app/modules/identity/google.py` | Google id-token verification and account linking |
| `app/modules/identity/sessions.py` | token issue, rotation, reuse detection, revocation |
| `app/api/deps.py` | `current_user`, `require_role` — shared by every audience |
| `app/api/student/auth.py` | the `/v1/app/auth/*` routes |
| `migrations/versions/*_identity.py` | the tables |

---

### Task 1: Role enum and identity models

**Files:**
- Create: `app/modules/identity/__init__.py` (empty for now)
- Create: `app/modules/identity/roles.py`
- Create: `app/modules/identity/models.py`
- Test: `tests/modules/identity/test_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/modules/__init__.py`, `tests/modules/identity/__init__.py` (both
empty) and `tests/modules/identity/test_models.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from app.core.ids import IdPrefix, parse_id
from app.modules.identity.models import RefreshToken, User, UserRole
from app.modules.identity.roles import Role


def test_role_enum_has_exactly_the_four_roles():
    assert {r.value for r in Role} == {"admin", "owner", "refiller", "student"}


def test_user_public_id_is_generated_and_prefixed():
    user = User(email="a@b.test", hashed_password="x")
    assert parse_id(user.public_id, IdPrefix.USER)


def test_two_users_get_different_public_ids():
    assert User(email="a@b.test", hashed_password="x").public_id != (
        User(email="c@d.test", hashed_password="x").public_id
    )


def test_user_defaults(db_session):
    user = User(email="a@b.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    assert user.is_active is True
    assert user.is_guest is False
    assert user.created_at is not None


def test_email_is_unique(db_session):
    db_session.add(User(email="dup@b.test", hashed_password="x"))
    db_session.flush()
    db_session.add(User(email="dup@b.test", hashed_password="y"))
    with pytest.raises(Exception):
        db_session.flush()


def test_a_user_cannot_hold_the_same_role_twice(db_session):
    user = User(email="r@b.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    db_session.add(UserRole(user_id=user.id, role=Role.STUDENT))
    db_session.flush()
    db_session.add(UserRole(user_id=user.id, role=Role.STUDENT))
    with pytest.raises(Exception):
        db_session.flush()


def test_refresh_token_table_has_no_column_that_could_hold_a_raw_token(db_session):
    """The table must be unable to store a usable token, not merely avoid it.

    Asserting on the column set rather than on an instance attribute: a model
    without the attribute proves nothing, since any attribute is absent until
    something adds it.
    """
    columns = set(RefreshToken.__table__.columns.keys())
    assert "token_hash" in columns
    assert not {"token", "raw_token", "secret", "value"} & columns


def test_refresh_token_hash_is_unique(db_session):
    user = User(email="t@b.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    expires = datetime.now(UTC) + timedelta(days=30)
    db_session.add(
        RefreshToken(user_id=user.id, token_hash="deadbeef", family_id="fam_1", expires_at=expires)
    )
    db_session.flush()
    db_session.add(
        RefreshToken(user_id=user.id, token_hash="deadbeef", family_id="fam_2", expires_at=expires)
    )
    with pytest.raises(Exception):
        db_session.flush()


def test_legacy_id_column_exists_for_migration(db_session):
    user = User(email="l@b.test", hashed_password="x", legacy_id=4321)
    db_session.add(user)
    db_session.flush()
    assert user.legacy_id == 4321
```

- [ ] **Step 2: Add the `db_session` fixture**

Append to `tests/conftest.py`:

```python
from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.core.db import Base, get_engine


@pytest.fixture
def db_session(postgres_url: str) -> Iterator[Session]:
    """A session on a schema built from the ORM metadata, rolled back after.

    Tables are created from Base.metadata rather than by running migrations, so
    a model test fails on the model rather than on a migration that has not been
    written yet. tests/test_migrations.py is what proves the two agree.
    """
    import app.modules.identity.models  # noqa: F401  (register the mappers)

    engine = get_engine(postgres_url)
    Base.metadata.create_all(engine)

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/modules -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.modules.identity'`

- [ ] **Step 4: Write `app/modules/identity/roles.py`**

```python
"""The four roles.

Three loose booleans on the user row is how the old backend expressed this, and
it is why a refiller could be one forgotten check away from money data. A role
is a row, and authorisation asks the role table.

`is_guest` is deliberately absent: a guest is a STUDENT who has not registered,
which is an account kind rather than a permission. `subscription_enabled` is
absent because it is a billing concern, not an identity one.
"""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    OWNER = "owner"
    REFILLER = "refiller"
    STUDENT = "student"
```

- [ ] **Step 5: Write `app/modules/identity/models.py`**

```python
"""Identity tables: users, their roles, and their refresh tokens.

Only this module may import these classes. Everything else goes through the
service functions, so the storage shape stays changeable.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.ids import IdPrefix, new_id
from app.modules.identity.roles import Role


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # What the API exposes. The integer primary key never leaves the database.
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.USER)
    )

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # An anonymous account created by /auth/guest. Not a role: guests hold
    # STUDENT like anyone else. Guests have no wallet.
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Row id in the backend being replaced. Nullable, indexed, kept permanently
    # so a number that looks wrong later can be traced to its origin.
    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    roles: Mapped[list["UserRole"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(String(20))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="roles")


class RefreshToken(Base):
    """A refresh token, stored only as a hash.

    `family_id` ties every token descended from one login together, so a replay
    can revoke the whole chain rather than just the token presented.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    family_id: Mapped[str] = mapped_column(String(32), index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 6: Run to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/modules -v`
Expected: `9 passed`

- [ ] **Step 7: Lint, full suite, commit**

```bash
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m pytest -q
git add app/modules tests/modules tests/conftest.py
git commit -m "feat(identity): user, role and refresh-token models"
```

---

### Task 2: Identity migration

**Files:**
- Modify: `migrations/env.py` (uncomment the identity models import)
- Create: `migrations/versions/*_identity.py` (autogenerated)

- [ ] **Step 1: Register the models with Alembic**

In `migrations/env.py`, replace the placeholder comment with a real import:

```python
from app.modules.identity import models as _identity_models  # noqa: F401
```

- [ ] **Step 2: Autogenerate**

```powershell
$env:DATABASE_URL = "postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo_test"
.venv\Scripts\alembic revision --autogenerate -m "identity"
```

- [ ] **Step 3: Read the generated migration before trusting it**

Open the new file and confirm it creates `users`, `user_roles`, `refresh_tokens`
with the unique constraints on `users.email`, `users.public_id`,
`refresh_tokens.token_hash` and `uq_user_role`. Autogenerate misses some things;
if a constraint is absent, add it by hand.

**Note:** `tests/core/test_db.py` and the `db_session` fixture call
`Base.metadata.create_all`, which can leave tables behind in
`printvendo_test`. Autogenerate diffs against the live database, so a dirty
test database produces an empty or wrong migration. Reset first:

```bash
PGPASSWORD=printvendo "/c/Program Files/PostgreSQL/18/bin/psql.exe" -U printvendo -h 127.0.0.1 \
  -d printvendo_test -c "drop schema public cascade; create schema public;"
```

Then re-run `alembic upgrade head` before autogenerating.

- [ ] **Step 4: Verify upgrade and no drift**

```bash
.venv/Scripts/python -m pytest tests/test_migrations.py -v
```

Expected: 3 passed. `test_autogenerate_detects_no_drift_after_upgrade` is the
one that matters — it proves the migration and the models agree.

- [ ] **Step 5: Commit**

```bash
git add migrations
git commit -m "feat(identity): create users, user_roles and refresh_tokens"
```

---

### Task 3: Repository

**Files:**
- Create: `app/modules/identity/repository.py`
- Test: `tests/modules/identity/test_repository.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.modules.identity import repository as repo
from app.modules.identity.models import User, UserRole
from app.modules.identity.roles import Role


@pytest.fixture
def user(db_session) -> User:
    u = User(email="Person@Example.test", hashed_password="x", full_name="Person")
    db_session.add(u)
    db_session.flush()
    return u


def test_get_by_email_is_case_insensitive(db_session, user):
    assert repo.get_by_email(db_session, "person@example.test") is not None
    assert repo.get_by_email(db_session, "PERSON@EXAMPLE.TEST") is not None


def test_get_by_email_returns_none_when_absent(db_session):
    assert repo.get_by_email(db_session, "nobody@example.test") is None


def test_get_by_public_id(db_session, user):
    assert repo.get_by_public_id(db_session, user.public_id).id == user.id


def test_get_by_public_id_rejects_a_malformed_id(db_session):
    assert repo.get_by_public_id(db_session, "not-an-id") is None


def test_get_by_public_id_rejects_an_id_of_the_wrong_kind(db_session, user):
    wrong_kind = user.public_id.replace("usr_", "ksk_")
    assert repo.get_by_public_id(db_session, wrong_kind) is None


def test_roles_of_returns_an_empty_set_for_a_new_user(db_session, user):
    assert repo.roles_of(db_session, user.id) == set()


def test_grant_role_is_idempotent(db_session, user):
    repo.grant_role(db_session, user.id, Role.STUDENT)
    repo.grant_role(db_session, user.id, Role.STUDENT)
    db_session.flush()

    assert repo.roles_of(db_session, user.id) == {Role.STUDENT}
    assert db_session.query(UserRole).filter_by(user_id=user.id).count() == 1


def test_grant_role_adds_to_existing_roles(db_session, user):
    repo.grant_role(db_session, user.id, Role.STUDENT)
    repo.grant_role(db_session, user.id, Role.OWNER)
    db_session.flush()
    assert repo.roles_of(db_session, user.id) == {Role.STUDENT, Role.OWNER}


def test_inactive_users_are_not_returned_by_email(db_session, user):
    user.is_active = False
    db_session.flush()
    assert repo.get_by_email(db_session, user.email) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/modules/identity/test_repository.py -v`
Expected: FAIL — no module `app.modules.identity.repository`

- [ ] **Step 3: Implement**

```python
"""Every query against the identity tables.

Route code never queries these tables directly; that is what keeps the storage
shape changeable and the authorisation rules in one place.

Emails are matched case-insensitively and stored as given. Postgres string
comparison is case-sensitive, so "Person@example.test" and
"person@example.test" are two different rows unless the query folds case --
which is how duplicate accounts appear.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.identity.models import User, UserRole
from app.modules.identity.roles import Role


def get_by_email(db: Session, email: str) -> User | None:
    """Active user with this email, case-insensitively."""
    stmt = select(User).where(
        func.lower(User.email) == email.strip().lower(),
        User.is_active.is_(True),
    )
    return db.execute(stmt).scalar_one_or_none()


def get_by_public_id(db: Session, public_id: str) -> User | None:
    """Active user with this public id, or None if the id is not a user id."""
    try:
        parse_id(public_id, IdPrefix.USER)
    except InvalidId:
        return None

    stmt = select(User).where(User.public_id == public_id, User.is_active.is_(True))
    return db.execute(stmt).scalar_one_or_none()


def email_exists(db: Session, email: str) -> bool:
    """Whether any row holds this email, active or not.

    Registration checks this rather than get_by_email: a deactivated account
    still owns its address, and the unique constraint would reject the insert
    anyway -- better a clear message than an integrity error.
    """
    stmt = select(User.id).where(func.lower(User.email) == email.strip().lower())
    return db.execute(stmt).first() is not None


def roles_of(db: Session, user_id: int) -> set[Role]:
    stmt = select(UserRole.role).where(UserRole.user_id == user_id)
    return {Role(r) for r in db.execute(stmt).scalars()}


def grant_role(db: Session, user_id: int, role: Role) -> None:
    """Give a user a role. Granting one they already hold is a no-op."""
    if role in roles_of(db, user_id):
        return
    db.add(UserRole(user_id=user_id, role=role))


def revoke_role(db: Session, user_id: int, role: Role) -> None:
    db.query(UserRole).filter(UserRole.user_id == user_id, UserRole.role == role).delete()
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/identity/test_repository.py -v   # 10 passed
.venv/Scripts/python -m ruff check .
git add app/modules/identity/repository.py tests/modules/identity/test_repository.py
git commit -m "feat(identity): repository with case-insensitive email lookup"
```

---

### Task 4: Registration and password login

**Files:**
- Create: `app/modules/identity/passwords.py`
- Test: `tests/modules/identity/test_passwords.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from passlib.hash import pbkdf2_sha256

from app.core.errors import Conflict, Unauthorized
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.passwords import authenticate, register
from app.modules.identity.roles import Role


def test_register_creates_a_student(db_session):
    user = register(db_session, "new@example.test", "correct horse battery", "New")
    db_session.flush()

    assert user.email == "new@example.test"
    assert repo.roles_of(db_session, user.id) == {Role.STUDENT}
    assert user.is_guest is False


def test_register_does_not_store_the_password(db_session):
    user = register(db_session, "new@example.test", "correct horse battery", None)
    assert "correct horse battery" not in user.hashed_password


def test_register_rejects_a_duplicate_email(db_session):
    register(db_session, "dup@example.test", "password one", None)
    db_session.flush()
    with pytest.raises(Conflict):
        register(db_session, "dup@example.test", "password two", None)


def test_register_rejects_a_duplicate_email_differing_only_by_case(db_session):
    register(db_session, "Dup@Example.test", "password one", None)
    db_session.flush()
    with pytest.raises(Conflict):
        register(db_session, "dup@example.TEST", "password two", None)


def test_authenticate_accepts_the_right_password(db_session):
    register(db_session, "a@example.test", "hunter2hunter2", None)
    db_session.flush()
    assert authenticate(db_session, "a@example.test", "hunter2hunter2") is not None


def test_authenticate_rejects_the_wrong_password(db_session):
    register(db_session, "a@example.test", "hunter2hunter2", None)
    db_session.flush()
    with pytest.raises(Unauthorized):
        authenticate(db_session, "a@example.test", "wrong")


def test_authenticate_rejects_an_unknown_email(db_session):
    with pytest.raises(Unauthorized):
        authenticate(db_session, "nobody@example.test", "whatever")


def test_the_two_failures_are_indistinguishable(db_session):
    """Different messages for "no such user" and "wrong password" let an
    attacker enumerate which emails have accounts."""
    register(db_session, "a@example.test", "hunter2hunter2", None)
    db_session.flush()

    with pytest.raises(Unauthorized) as wrong_password:
        authenticate(db_session, "a@example.test", "wrong")
    with pytest.raises(Unauthorized) as no_such_user:
        authenticate(db_session, "nobody@example.test", "wrong")

    assert str(wrong_password.value) == str(no_such_user.value)


def test_a_legacy_pbkdf2_hash_is_upgraded_on_successful_login(db_session):
    """Migrated users arrive with pbkdf2 hashes; logging in moves them to bcrypt."""
    user = User(email="old@example.test", hashed_password=pbkdf2_sha256.hash("legacy pass"))
    db_session.add(user)
    db_session.flush()
    assert user.hashed_password.startswith("$pbkdf2-sha256$")

    authenticate(db_session, "old@example.test", "legacy pass")
    db_session.flush()

    assert user.hashed_password.startswith("$2")
    assert authenticate(db_session, "old@example.test", "legacy pass") is not None


def test_a_failed_login_does_not_rehash(db_session):
    user = User(email="old@example.test", hashed_password=pbkdf2_sha256.hash("legacy pass"))
    db_session.add(user)
    db_session.flush()
    before = user.hashed_password

    with pytest.raises(Unauthorized):
        authenticate(db_session, "old@example.test", "wrong")

    assert user.hashed_password == before
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — no module `app.modules.identity.passwords`

- [ ] **Step 3: Implement**

```python
"""Registration and email/password sign-in.

Two rules here are security properties rather than conveniences:

* A wrong password and an unknown email produce the *same* error. Different
  messages let anyone enumerate which addresses have accounts.
* A successful login re-hashes when the stored hash uses a legacy scheme. Every
  user migrated from the old backend arrives with a pbkdf2_sha256 hash, and
  this is what drains them to bcrypt without a password reset.
"""

from sqlalchemy.orm import Session

from app.core.errors import Conflict, Unauthorized
from app.core.security import hash_password, needs_rehash, verify_password
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

BAD_CREDENTIALS = "That email and password do not match an account."


def register(db: Session, email: str, password: str, full_name: str | None) -> User:
    email = email.strip()
    if repo.email_exists(db, email):
        raise Conflict("An account with that email already exists.")

    user = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
    )
    db.add(user)
    db.flush()  # assign user.id so the role row can reference it

    repo.grant_role(db, user.id, Role.STUDENT)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    user = repo.get_by_email(db, email)

    if user is None or not verify_password(password, user.hashed_password):
        raise Unauthorized(BAD_CREDENTIALS)

    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)
        db.add(user)

    return user
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/identity/test_passwords.py -v   # 10 passed
.venv/Scripts/python -m ruff check .
git add app/modules/identity/passwords.py tests/modules/identity/test_passwords.py
git commit -m "feat(identity): registration and password login with legacy hash upgrade"
```

> **Note on timing:** returning early when the user is absent means an unknown
> email answers faster than a wrong password, which is a timing oracle. It is
> not addressed here because the endpoint is rate-limited to 5/min and the
> signal is small relative to network noise. If you decide to close it, do it by
> always running a verify against a dummy hash — do not pretend a comment fixes
> it.

---

### Task 5: Guest accounts

**Files:**
- Create: `app/modules/identity/guests.py`
- Test: `tests/modules/identity/test_guests.py`

- [ ] **Step 1: Write the failing test**

```python
import re

from app.modules.identity import repository as repo
from app.modules.identity.guests import create_guest
from app.modules.identity.roles import Role

GUEST_EMAIL = re.compile(r"^guest_[0-9a-f]{32}@guest\.printvendo$")


def test_guest_is_flagged_as_a_guest(db_session):
    guest = create_guest(db_session)
    db_session.flush()
    assert guest.is_guest is True


def test_guest_holds_the_student_role(db_session):
    guest = create_guest(db_session)
    db_session.flush()
    assert repo.roles_of(db_session, guest.id) == {Role.STUDENT}


def test_guest_email_follows_the_synthetic_pattern(db_session):
    guest = create_guest(db_session)
    assert GUEST_EMAIL.match(guest.email), guest.email


def test_guests_get_distinct_emails(db_session):
    emails = {create_guest(db_session).email for _ in range(50)}
    assert len(emails) == 50


def test_guest_password_is_random_and_unusable(db_session):
    a = create_guest(db_session)
    b = create_guest(db_session)
    assert a.hashed_password != b.hashed_password
    assert a.hashed_password.startswith("$2")
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

```python
"""Anonymous guest accounts.

A student can print without creating an account. The guest is a real user row
holding STUDENT, flagged is_guest, with a synthetic email and a random password
nobody knows -- so it can never be signed into again, only carried by the tokens
issued at creation.

Guests have no wallet. That rule lives in the wallet module; identity only
records the flag.
"""

import secrets

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

GUEST_EMAIL_DOMAIN = "guest.printvendo"


def create_guest(db: Session) -> User:
    user = User(
        email=f"guest_{secrets.token_hex(16)}@{GUEST_EMAIL_DOMAIN}",
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        full_name="Guest",
        is_guest=True,
    )
    db.add(user)
    db.flush()

    repo.grant_role(db, user.id, Role.STUDENT)
    return user
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/identity/test_guests.py -v   # 5 passed
git add app/modules/identity/guests.py tests/modules/identity/test_guests.py
git commit -m "feat(identity): anonymous guest accounts"
```

---

### Task 6: Sessions — issue, rotate, reuse detection

This is the security-critical task. Read D-I1 above before starting.

**Files:**
- Create: `app/modules/identity/sessions.py`
- Test: `tests/modules/identity/test_sessions.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import Unauthorized
from app.core.security import TokenType, decode_token
from app.modules.identity.models import RefreshToken, User
from app.modules.identity.sessions import (
    GRACE_SECONDS,
    issue_tokens,
    revoke_all,
    revoke_refresh,
    rotate_refresh,
)

SECRET = "s" * 32


@pytest.fixture
def user(db_session) -> User:
    u = User(email="s@example.test", hashed_password="x")
    db_session.add(u)
    db_session.flush()
    return u


def _row(db_session, token: str) -> RefreshToken:
    from app.modules.identity.sessions import _hash

    return db_session.query(RefreshToken).filter_by(token_hash=_hash(token)).one()


def test_issue_returns_an_access_token_carrying_the_public_id(db_session, user):
    access, _ = issue_tokens(db_session, user, SECRET)
    assert decode_token(access, TokenType.ACCESS, SECRET).subject == user.public_id


def test_issue_stores_only_a_hash_of_the_refresh_token(db_session, user):
    _, refresh = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    assert db_session.query(RefreshToken).filter_by(token_hash=refresh).count() == 0
    assert _row(db_session, refresh) is not None


def test_rotation_issues_a_new_refresh_token(db_session, user):
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    _, second = rotate_refresh(db_session, first, SECRET)
    assert second != first


def test_rotation_keeps_the_family(db_session, user):
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    _, second = rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    assert _row(db_session, second).family_id == _row(db_session, first).family_id


def test_a_concurrent_refresh_inside_the_grace_window_succeeds(db_session, user):
    """Two tabs refreshing at once must both work.

    Revoking instantly is what caused the old backend's "logs out frequently"
    bug: the losing tab was told its token was invalid and the client bounced
    the user to /login.
    """
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    # Same token again, immediately -- the other tab.
    access, again = rotate_refresh(db_session, first, SECRET)
    assert access
    assert again


def test_a_replay_after_the_grace_window_is_refused(db_session, user):
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    row = _row(db_session, first)
    row.revoked_at = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 5)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)


def test_a_replay_after_the_grace_window_kills_the_whole_family(db_session, user):
    """A token replayed long after rotation means it was stolen. Every
    descendant of that login must die, not just the one presented."""
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    _, second = rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    row = _row(db_session, first)
    row.revoked_at = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 5)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    # The still-live descendant must now be dead too.
    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, second, SECRET)


def test_an_expired_token_is_refused_even_inside_the_grace_window(db_session, user):
    """Grace covers revocation, never expiry. Expiry is the real deadline."""
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    row = _row(db_session, first)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)


def test_an_unknown_token_is_refused(db_session):
    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, "never-issued", SECRET)


def test_logout_revokes_immediately_with_no_grace(db_session, user):
    """Someone signing out on a shared machine must be signed out now, not in
    a minute."""
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    revoke_refresh(db_session, first)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)


def test_revoke_all_kills_every_session(db_session, user):
    _, a = issue_tokens(db_session, user, SECRET)
    _, b = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    revoke_all(db_session, user.id)
    db_session.flush()

    for token in (a, b):
        with pytest.raises(Unauthorized):
            rotate_refresh(db_session, token, SECRET)


def test_separate_logins_are_separate_families(db_session, user):
    _, a = issue_tokens(db_session, user, SECRET)
    _, b = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    assert _row(db_session, a).family_id != _row(db_session, b).family_id
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

```python
"""Session tokens: issue, rotate, detect replay, revoke.

Refresh tokens are opaque random strings stored only as SHA-256 hashes, so a
database dump cannot be used to impersonate anyone. Each login starts a family;
rotation keeps the family id, which is what lets a replay revoke the entire
chain rather than only the token presented.

The grace window is load-bearing. See D-I1 in the plan: revoking instantly on
rotation makes two concurrent refreshes race, and the loser gets signed out.
Grace applies to *revocation* only -- an expired token is always refused,
because expiry is the real deadline.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Unauthorized
from app.core.security import TokenType, create_token
from app.modules.identity.models import RefreshToken, User

ACCESS_LIFETIME = timedelta(minutes=15)
REFRESH_LIFETIME = timedelta(days=30)

# How long a rotated token keeps working. Long enough for a burst of parallel
# requests from several tabs, short enough to be useless to an attacker who
# steals a token and replays it later.
GRACE_SECONDS = 60

INVALID_SESSION = "Your session has expired. Please sign in again."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_refresh(db: Session, user_id: int, family_id: str) -> str:
    token = secrets.token_urlsafe(48)
    db.add(
        RefreshToken(
            user_id=user_id,
            token_hash=_hash(token),
            family_id=family_id,
            expires_at=datetime.now(UTC) + REFRESH_LIFETIME,
        )
    )
    return token


def issue_tokens(db: Session, user: User, secret: str) -> tuple[str, str]:
    """Start a new session. Returns (access token, refresh token)."""
    family_id = secrets.token_hex(16)
    access = create_token(user.public_id, TokenType.ACCESS, secret, ACCESS_LIFETIME)
    refresh = _new_refresh(db, user.id, family_id)
    return access, refresh


def _lookup(db: Session, token: str) -> RefreshToken | None:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == _hash(token))
    return db.execute(stmt).scalar_one_or_none()


def revoke_family(db: Session, family_id: str) -> None:
    """Kill every token descended from one login, with no grace."""
    dead = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 1)
    db.query(RefreshToken).filter(RefreshToken.family_id == family_id).update(
        {"revoked_at": dead}
    )


def rotate_refresh(db: Session, token: str, secret: str) -> tuple[str, str]:
    """Exchange a refresh token for a fresh pair.

    Raises Unauthorized for an unknown, expired or replayed token.
    """
    row = _lookup(db, token)
    if row is None:
        raise Unauthorized(INVALID_SESSION)

    now = datetime.now(UTC)

    if row.expires_at <= now:
        raise Unauthorized(INVALID_SESSION)

    if row.revoked_at is not None:
        age = now - row.revoked_at
        if age > timedelta(seconds=GRACE_SECONDS):
            # Presented long after it was rotated away: this token was kept,
            # which means it was stolen. Kill the whole chain.
            revoke_family(db, row.family_id)
            raise Unauthorized(INVALID_SESSION)
        # Inside the window: a concurrent refresh from another tab.

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise Unauthorized(INVALID_SESSION)

    row.revoked_at = row.revoked_at or now
    db.add(row)

    access = create_token(user.public_id, TokenType.ACCESS, secret, ACCESS_LIFETIME)
    refresh = _new_refresh(db, user.id, row.family_id)
    return access, refresh


def revoke_refresh(db: Session, token: str) -> None:
    """Sign out. No grace -- a shared machine must be signed out now."""
    row = _lookup(db, token)
    if row is None:
        return
    row.revoked_at = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 1)
    db.add(row)


def revoke_all(db: Session, user_id: int) -> None:
    """Kill every session a user has, on every device."""
    dead = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 1)
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update(
        {"revoked_at": dead}
    )
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/identity/test_sessions.py -v   # 13 passed
.venv/Scripts/python -m ruff check .
git add app/modules/identity/sessions.py tests/modules/identity/test_sessions.py
git commit -m "feat(identity): refresh rotation with reuse detection and concurrency grace"
```

---

### Task 7: Google sign-in

**Files:**
- Create: `app/modules/identity/google.py`
- Modify: `pyproject.toml` (add `google-auth`)
- Test: `tests/modules/identity/test_google.py`

- [ ] **Step 1: Add the dependency**

Add `"google-auth>=2.35"` to `dependencies` in `pyproject.toml`, then
`.venv/Scripts/pip install -e ".[dev]"`.

- [ ] **Step 2: Write the failing test**

Google verification is mocked — these tests must not reach the network.

```python
import pytest

from app.core.errors import BadRequest
from app.modules.identity import repository as repo
from app.modules.identity.google import sign_in_with_google
from app.modules.identity.models import User
from app.modules.identity.roles import Role


@pytest.fixture
def verifier():
    """Stands in for Google's token verification."""

    def _verify(token: str, client_id: str) -> dict:
        if token == "good":
            return {"email": "g@example.test", "name": "Gee"}
        if token == "no-email":
            return {"name": "Gee"}
        raise ValueError("Invalid token")

    return _verify


def test_creates_an_account_on_first_sign_in(db_session, verifier):
    user = sign_in_with_google(db_session, "good", "client-id", verifier)
    db_session.flush()

    assert user.email == "g@example.test"
    assert user.full_name == "Gee"
    assert repo.roles_of(db_session, user.id) == {Role.STUDENT}


def test_reuses_the_existing_account_on_later_sign_ins(db_session, verifier):
    first = sign_in_with_google(db_session, "good", "client-id", verifier)
    db_session.flush()
    second = sign_in_with_google(db_session, "good", "client-id", verifier)

    assert first.id == second.id


def test_links_to_an_account_registered_by_password(db_session, verifier):
    existing = User(email="g@example.test", hashed_password="x")
    db_session.add(existing)
    db_session.flush()

    user = sign_in_with_google(db_session, "good", "client-id", verifier)
    assert user.id == existing.id


def test_an_invalid_token_is_rejected(db_session, verifier):
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "bad", "client-id", verifier)


def test_a_token_without_an_email_is_rejected(db_session, verifier):
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "no-email", "client-id", verifier)


def test_sign_in_is_refused_when_google_is_not_configured(db_session, verifier):
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "good", "", verifier)


def test_a_created_account_gets_an_unusable_password(db_session, verifier):
    user = sign_in_with_google(db_session, "good", "client-id", verifier)
    assert user.hashed_password
    assert user.hashed_password.startswith("$2")
```

- [ ] **Step 3: Run to verify it fails**

- [ ] **Step 4: Implement**

```python
"""Google sign-in.

The id token is verified locally with google-auth against Google's cached
public keys. The old backend's earlier approach put the raw token in a URL,
which leaks it into every log along the way.

The verifier is injected so tests can exercise this without the network. The
default is the real one.
"""

import secrets
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.security import hash_password
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

Verifier = Callable[[str, str], dict]


def verify_with_google(token: str, client_id: str) -> dict:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        token,
        google_requests.Request(),
        client_id,
        clock_skew_in_seconds=10,
    )


def sign_in_with_google(
    db: Session,
    token: str,
    client_id: str,
    verifier: Verifier = verify_with_google,
) -> User:
    if not client_id:
        raise BadRequest("Google sign-in is not available right now.")

    try:
        claims = verifier(token, client_id)
    except ValueError as exc:
        raise BadRequest("That Google sign-in could not be verified.") from exc
    except Exception as exc:  # noqa: BLE001 - google-auth raises broadly
        raise BadRequest("That Google sign-in could not be verified.") from exc

    email = claims.get("email")
    if not email:
        raise BadRequest("That Google account has no email address.")

    user = repo.get_by_email(db, email)
    if user is not None:
        return user

    # Password is random and never told to anyone: this account signs in with
    # Google. It is set rather than left null so the column stays NOT NULL and
    # every code path can assume a hash is present.
    user = User(
        email=email,
        full_name=claims.get("name"),
        hashed_password=hash_password(secrets.token_urlsafe(32)),
    )
    db.add(user)
    db.flush()
    repo.grant_role(db, user.id, Role.STUDENT)
    return user
```

- [ ] **Step 5: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/modules/identity/test_google.py -v   # 7 passed
git add app/modules/identity/google.py tests/modules/identity/test_google.py pyproject.toml
git commit -m "feat(identity): Google sign-in with locally verified id tokens"
```

---

### Task 8: Request dependencies — current_user and role guards

**Files:**
- Create: `app/api/deps.py`
- Test: `tests/api/test_deps.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, require_role
from app.core.errors import install_error_handlers
from app.core.ids import IdPrefix, new_id
from app.core.security import TokenType, create_token
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

SECRET = "s" * 32


def _access(public_id: str) -> str:
    return create_token(public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5))


def _auth(public_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_access(public_id)}"}


@pytest.fixture
def student(db_session) -> User:
    user = User(email="s@example.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    repo.grant_role(db_session, user.id, Role.STUDENT)
    db_session.flush()
    return user


@pytest.fixture
def app(db_session) -> FastAPI:
    from app.api.deps import get_db, get_secret

    application = FastAPI()
    install_error_handlers(application)
    application.dependency_overrides[get_db] = lambda: db_session
    application.dependency_overrides[get_secret] = lambda: SECRET

    @application.get("/whoami")
    def whoami(user: CurrentUser) -> dict:
        return {"public_id": user.public_id}

    @application.get("/admin-only", dependencies=[Depends(require_role(Role.ADMIN))])
    def admin_only() -> dict:
        return {"ok": True}

    return application


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_a_valid_token_resolves_the_user(app, student):
    response = _client(app).get("/whoami", headers=_auth(student.public_id))
    assert response.status_code == 200
    assert response.json()["public_id"] == student.public_id


def test_a_missing_header_is_401(app):
    assert _client(app).get("/whoami").status_code == 401


def test_a_malformed_header_is_401(app):
    response = _client(app).get("/whoami", headers={"Authorization": "not-bearer"})
    assert response.status_code == 401


def test_a_garbage_token_is_401(app):
    response = _client(app).get("/whoami", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401


def test_a_refresh_token_is_not_accepted_as_an_access_token(app, student):
    token = create_token(student.public_id, TokenType.REFRESH, SECRET, timedelta(days=1))
    response = _client(app).get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_token_for_a_user_who_does_not_exist_is_401(app):
    response = _client(app).get("/whoami", headers=_auth(new_id(IdPrefix.USER)))
    assert response.status_code == 401


def test_a_token_carrying_a_kiosk_id_instead_of_a_user_id_is_401(app):
    """parse_id in the repository is what refuses this, not a database miss."""
    response = _client(app).get("/whoami", headers=_auth(new_id(IdPrefix.KIOSK)))
    assert response.status_code == 401


def test_an_inactive_user_is_401(app, db_session, student):
    student.is_active = False
    db_session.flush()
    response = _client(app).get("/whoami", headers=_auth(student.public_id))
    assert response.status_code == 401


def test_role_guard_refuses_a_user_without_the_role(app, student):
    response = _client(app).get("/admin-only", headers=_auth(student.public_id))
    assert response.status_code == 403


def test_role_guard_admits_a_user_with_the_role(app, db_session, student):
    repo.grant_role(db_session, student.id, Role.ADMIN)
    db_session.flush()

    response = _client(app).get("/admin-only", headers=_auth(student.public_id))
    assert response.status_code == 200
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

```python
"""Request-scoped dependencies shared by every audience.

There is exactly one place that turns a bearer token into a user, and exactly
one that checks a role. The old backend had a per-router auth dependency, which
is how /owner/* ended up admin-only with a "DO NOT LOOSEN" comment instead of a
check -- there was no single place to put the check.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import get_session_factory
from app.core.errors import Forbidden, Unauthorized
from app.core.security import TokenError, TokenType, decode_token
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

NOT_SIGNED_IN = "You need to sign in to do that."


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


def get_secret(settings: Annotated[Settings, Depends(get_settings_from_app)]) -> str:
    return settings.JWT_SECRET_KEY


def get_db(
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> Iterator[Session]:
    session = get_session_factory(settings.DATABASE_URL)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized(NOT_SIGNED_IN)
    return token


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
) -> User:
    token = _bearer_token(request)

    try:
        claims = decode_token(token, TokenType.ACCESS, secret)
    except TokenError as exc:
        raise Unauthorized(NOT_SIGNED_IN) from exc

    user = repo.get_by_public_id(db, claims.subject)
    if user is None:
        raise Unauthorized(NOT_SIGNED_IN)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(role: Role):
    """Dependency factory: refuse anyone who does not hold `role`."""

    def _guard(
        user: CurrentUser,
        db: Annotated[Session, Depends(get_db)],
    ) -> User:
        if role not in repo.roles_of(db, user.id):
            raise Forbidden("You do not have access to that.")
        return user

    return _guard
```

- [ ] **Step 4: Verify, lint, commit**

```bash
.venv/Scripts/python -m pytest tests/api/test_deps.py -v   # 10 passed
git add app/api/deps.py tests/api
git commit -m "feat(api): single current_user resolver and role guard"
```

Create `tests/api/__init__.py` (empty) if pytest cannot import the package.

---

### Task 9: The `/v1/app/auth/*` routes

**Files:**
- Create: `app/api/student/__init__.py`
- Create: `app/api/student/auth.py`
- Modify: `app/main.py` (mount the router)
- Modify: `tests/authz/matrix.py` (declare the new routes)
- Test: `tests/api/test_auth_routes.py`

- [ ] **Step 1: Declare the routes in the authorisation matrix first**

The harness fails the build for any undeclared route, so this comes first —
that is the whole point of it. Add to `MATRIX` in `tests/authz/matrix.py`:

```python
    ("POST", "/v1/app/auth/register"): {PUBLIC},
    ("POST", "/v1/app/auth/login"): {PUBLIC},
    ("POST", "/v1/app/auth/guest"): {PUBLIC},
    ("POST", "/v1/app/auth/google"): {PUBLIC},
    ("POST", "/v1/app/auth/refresh"): {PUBLIC},
    ("POST", "/v1/app/auth/logout"): {PUBLIC},
    ("GET", "/v1/app/auth/me"): {STUDENT, OWNER, REFILLER, ADMIN},
```

`/refresh` and `/logout` are `PUBLIC` because they authenticate with the refresh
cookie, not a bearer token — the endpoint is reachable without an access token
by design.

- [ ] **Step 2: Write the failing test**

```python
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_secret
from app.core.config import Settings
from app.main import create_app

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY="k" * 44,
    CORS_ORIGINS="http://localhost:3000",
)


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    return TestClient(app, raise_server_exceptions=False)


def test_register_then_login(client):
    registered = client.post(
        "/v1/app/auth/register",
        json={"email": "new@example.test", "password": "correct horse battery", "full_name": "New"},
    )
    assert registered.status_code == 201, registered.text

    logged_in = client.post(
        "/v1/app/auth/login",
        json={"email": "new@example.test", "password": "correct horse battery"},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["access_token"]


def test_register_rejects_a_duplicate_with_409(client):
    body = {"email": "dup@example.test", "password": "correct horse battery"}
    client.post("/v1/app/auth/register", json=body)
    again = client.post("/v1/app/auth/register", json=body)
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


def test_login_with_a_wrong_password_is_401(client):
    client.post(
        "/v1/app/auth/register",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    response = client.post(
        "/v1/app/auth/login", json={"email": "a@example.test", "password": "nope"}
    )
    assert response.status_code == 401


def test_the_refresh_token_is_never_in_the_response_body(client):
    response = client.post(
        "/v1/app/auth/register",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    assert "refresh" not in response.text.lower()


def test_the_refresh_cookie_is_httponly(client):
    client.post(
        "/v1/app/auth/register",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    response = client.post(
        "/v1/app/auth/login",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_guest_login_returns_a_token_and_flags_the_account(client):
    response = client.post("/v1/app/auth/guest")
    assert response.status_code == 200
    assert response.json()["is_guest"] is True


def test_me_requires_a_token(client):
    assert client.get("/v1/app/auth/me").status_code == 401


def test_me_returns_the_signed_in_user(client):
    client.post(
        "/v1/app/auth/register",
        json={"email": "a@example.test", "password": "correct horse battery", "full_name": "Ay"},
    )
    login = client.post(
        "/v1/app/auth/login",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    token = login.json()["access_token"]

    me = client.get("/v1/app/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "a@example.test"
    assert body["roles"] == ["student"]
    assert body["id"].startswith("usr_")


def test_me_never_exposes_the_password_hash_or_row_id(client):
    client.post(
        "/v1/app/auth/register",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    login = client.post(
        "/v1/app/auth/login",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    me = client.get(
        "/v1/app/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert "hashed_password" not in me.text
    assert "legacy_id" not in me.text


def test_refresh_without_a_cookie_is_401(client):
    assert client.post("/v1/app/auth/refresh").status_code == 401


def test_refresh_with_the_cookie_returns_a_new_access_token(client):
    client.post(
        "/v1/app/auth/register",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    client.post(
        "/v1/app/auth/login",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    # TestClient keeps cookies between calls on the same instance.
    refreshed = client.post("/v1/app/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


def test_logout_clears_the_cookie(client):
    client.post(
        "/v1/app/auth/register",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    client.post(
        "/v1/app/auth/login",
        json={"email": "a@example.test", "password": "correct horse battery"},
    )
    response = client.post("/v1/app/auth/logout")
    assert response.status_code == 204
```

- [ ] **Step 3: Run to verify it fails**

- [ ] **Step 4: Implement `app/api/student/auth.py`**

```python
"""Sign-in for the student app.

The refresh token goes in an httpOnly cookie and never appears in a response
body -- a token in JSON is readable by any script on the page. The access token
does go in the body, because the client must attach it to each request; that is
why it lives fifteen minutes.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_db, get_secret, get_settings_from_app
from app.core.config import Settings
from app.core.errors import Unauthorized
from app.modules.identity import repository as repo
from app.modules.identity.google import sign_in_with_google
from app.modules.identity.guests import create_guest
from app.modules.identity.passwords import authenticate, register
from app.modules.identity.sessions import (
    REFRESH_LIFETIME,
    issue_tokens,
    revoke_refresh,
    rotate_refresh,
)

router = APIRouter(prefix="/v1/app/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
NO_SESSION = "You need to sign in to do that."


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleRequest(BaseModel):
    id_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_guest: bool = False


class MeResponse(BaseModel):
    id: str
    email: str
    full_name: str | None
    is_guest: bool
    roles: list[str]


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.ENV != "dev",
        samesite="lax",
        max_age=int(REFRESH_LIFETIME.total_seconds()),
        path="/v1/app/auth",
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register_account(
    payload: RegisterRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> TokenResponse:
    user = register(db, payload.email, payload.password, payload.full_name)
    access, refresh = issue_tokens(db, user, secret)
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(access_token=access)


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> TokenResponse:
    user = authenticate(db, payload.email, payload.password)
    access, refresh = issue_tokens(db, user, secret)
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(access_token=access, is_guest=user.is_guest)


@router.post("/guest", response_model=TokenResponse)
def guest_login(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> TokenResponse:
    user = create_guest(db)
    access, refresh = issue_tokens(db, user, secret)
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(access_token=access, is_guest=True)


@router.post("/google", response_model=TokenResponse)
def google_login(
    payload: GoogleRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> TokenResponse:
    user = sign_in_with_google(db, payload.id_token, settings.GOOGLE_CLIENT_ID)
    access, refresh = issue_tokens(db, user, secret)
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(access_token=access)


@router.post("/refresh", response_model=TokenResponse)
def refresh_session(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenResponse:
    if not refresh_token:
        raise Unauthorized(NO_SESSION)

    access, new_refresh = rotate_refresh(db, refresh_token, secret)
    _set_refresh_cookie(response, new_refresh, settings)
    return TokenResponse(access_token=access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    db: Annotated[Session, Depends(get_db)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> Response:
    if refresh_token:
        revoke_refresh(db, refresh_token)

    # Build the response we actually return and clear the cookie on *that*.
    # Taking an injected `response` and then returning a different Response
    # object silently discards the cookie deletion, leaving the browser holding
    # a revoked token and the user apparently still signed in.
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(REFRESH_COOKIE, path="/v1/app/auth")
    return response


@router.get("/me", response_model=MeResponse)
def me(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    return MeResponse(
        id=user.public_id,
        email=user.email,
        full_name=user.full_name,
        is_guest=user.is_guest,
        roles=sorted(r.value for r in repo.roles_of(db, user.id)),
    )
```

Note `_bearer_token` is not used by `/refresh` — it reads the cookie, which is
why the matrix marks it `PUBLIC`.

- [ ] **Step 5: Mount the router in `app/main.py`**

Inside `create_app`, after `install_error_handlers(app)`:

```python
    from app.api.student import auth as student_auth

    app.include_router(student_auth.router)
```

The import is inside the function deliberately: at module scope it would make
`app.main` import the whole route tree, and `tests/authz` builds an app purely
to enumerate routes.

- [ ] **Step 6: Verify**

```bash
.venv/Scripts/python -m pytest tests/api/test_auth_routes.py -v   # 12 passed
.venv/Scripts/python -m pytest tests/authz -v                     # matrix satisfied
.venv/Scripts/python -m pytest -q
.venv/Scripts/lint-imports
.venv/Scripts/python -m ruff check .
```

`lint-imports` must still report all contracts kept. If it reports a break,
`app/api` has reached into a module's ORM models — fix the direction, do not
relax the contract.

- [ ] **Step 7: Prove the matrix still bites**

Temporarily remove one line from `MATRIX` and confirm `pytest tests/authz`
fails naming that route. Restore it.

- [ ] **Step 8: Commit**

```bash
git add app/api app/main.py tests/api tests/authz
git commit -m "feat(api): student auth routes at /v1/app/auth"
```

---

### Task 10: Module surface and documentation

**Files:**
- Modify: `app/modules/identity/__init__.py`
- Modify: `printvendo-backend/CLAUDE.md`
- Modify: `.importlinter`

- [ ] **Step 1: Publish the module's surface**

`app/modules/identity/__init__.py`:

```python
"""The identity bounded context.

Import from here, never from the submodules: what this file exports is the
contract, and everything else is free to change. Nothing outside this package
may import identity.models.
"""

from app.modules.identity.google import sign_in_with_google
from app.modules.identity.guests import create_guest
from app.modules.identity.passwords import authenticate, register
from app.modules.identity.roles import Role
from app.modules.identity.sessions import (
    issue_tokens,
    revoke_all,
    revoke_refresh,
    rotate_refresh,
)

__all__ = [
    "Role",
    "authenticate",
    "create_guest",
    "issue_tokens",
    "register",
    "revoke_all",
    "revoke_refresh",
    "rotate_refresh",
    "sign_in_with_google",
]
```

- [ ] **Step 2: Add the independence contract**

Now that a module exists, add to `.importlinter`:

```ini
[importlinter:contract:modules-are-independent]
name = bounded contexts do not reach into each other
type = independence
modules =
    app.modules.identity
```

Later plans append each new module to this list. Verify with
`.venv/Scripts/lint-imports` — expect `4 kept, 0 broken`, and update the count
asserted in `tests/test_architecture.py`.

- [ ] **Step 3: Update `CLAUDE.md`**

Replace the Status section with the identity module's state, and add to the
conventions list:

```markdown
- **Refresh rotation has a 60-second grace window** (`identity/sessions.py`).
  Removing it reintroduces the old backend's "logs out frequently" bug, where
  two tabs refreshing at once signed the user out. Reuse *after* the window is
  treated as theft and revokes the whole token family.
- **`app.core.security` accepts `pbkdf2_sha256`** because every migrated user
  has one. Login re-hashes to bcrypt. Do not drop the legacy scheme until the
  migration is long done and no such hashes remain.
```

- [ ] **Step 4: Full verification and commit**

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/lint-imports
.venv/Scripts/python -m ruff check .
git add app/modules/identity/__init__.py .importlinter CLAUDE.md tests/test_architecture.py
git commit -m "docs(identity): module surface, independence contract, conventions"
```

---

## Outcome (completed 2026-08-14)

All tasks done. **209 tests passing**, 5 import contracts kept, ruff clean,
working tree clean. Verified end to end against a real uvicorn process and real
Postgres: register → verification email → verify → login → refresh → logout,
including a replayed verification token being refused.

**Scope changed mid-build at the user's request:** guests stay as they are (no
guest→real merge), and registration now requires email verification. Decisions
taken while adding it — unverified users may still sign in, Google sign-in
counts as verified, and migrated production users are grandfathered verified —
are recorded in `app/modules/identity/models.py` and in the migration's
docstring, because the data migration depends on the last one.

Defects found and fixed during implementation:

| # | Defect | Why it mattered |
|---|---|---|
| 1 | **The authorisation harness saw no router routes** | `include_router` does not flatten into `app.routes`; the `isinstance(route, APIRoute)` filter skipped every real route. The whole guardrail passed while checking nothing. |
| 2 | `logout` cleared the cookie on an injected `Response` then returned a different one | The deletion was discarded; the browser kept a revoked token and looked signed in. |
| 3 | `app.*` loggers were silent under uvicorn | `LoggingNotifier` claimed a developer could finish verification locally by reading the log. The line never appeared. |
| 4 | `pytest.raises(Exception)` in model tests | Would pass on a typo raising `AttributeError` — proved nothing about the constraint. Now `IntegrityError`. |
| 5 | `create_all` never adds columns to an existing table | A model change left every test running against the old shape. Schema is now dropped and rebuilt once per session. |
| 6 | Migration tests collided with the `db_session` fixture's tables | `alembic upgrade head` met tables that already existed; failure was about test ordering, not the migration. |
| 7 | A test asserting `not hasattr(token, "token")` | Trivially true for any model. Rewritten to assert the column set. |

Guardrails verified by deliberately breaking them:

- **Grace window**: `GRACE_SECONDS = 0` fails exactly one test — the concurrent
  refresh — reproducing the old backend's logout bug, and no others, confirming
  it does not weaken replay detection.
- **Authorisation matrix**: removing `/login` from `MATRIX` fails the build
  naming that route.
- **ORM boundary**: a direct `from app.modules.identity.models import ...` in
  `app/api` breaks the contract, while calling services stays legal.
- **Deactivated accounts**: the explicit `is_active` check in `authenticate`
  fails its test when removed, even though the repository also filters.

A background security review flagged `authenticate` as an authentication bypass
for deactivated accounts. That was a **false positive** — `repo.get_by_email`
already filters `is_active`. The check was added anyway, with a test that pins
it independently, because the property should not depend on a detail of another
function.

## Done when

- A student can register, log in, sign in with Google, or continue as a guest
- Access tokens carry `usr_…`, never a row id
- Refresh tokens live only as hashes, only in an httpOnly cookie
- Two tabs refreshing at once both succeed; a token replayed later kills the family
- A migrated `pbkdf2_sha256` password still works and upgrades on login
- Every new route is declared in `tests/authz/matrix.py`
- `lint-imports` reports 4 contracts kept, 0 broken
- `pytest -q` and `ruff check .` both clean

## Next plan

Sub-project 3, **kiosks** — registry, `kiosk_type`, onboarding stages, pricing,
paper, assignments, and the refiller invite flow from spec §6. It is the first
module with real per-scope authorisation, so it is where the `kiosk_scope`
resolver gets built and the authorisation matrix starts earning its keep.
