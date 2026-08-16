# Guest Login — Backend (Plan A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Add anonymous guest accounts: `is_guest` flag, `POST /auth/guest` (rate-limited),
a `get_non_guest_user` authorization dependency, and wallet endpoints that 403 guests.

**Architecture:** A guest is a real `User` row (`is_guest=true`, synthetic email, random
password) issued the same JWT + refresh cookie as `/login`. Wallet is revoked **server-side**
via a `get_non_guest_user` dependency on all `/wallet/*` routes. Role-gated (admin/owner)
endpoints already exclude guests.

**Tech Stack:** FastAPI, SQLAlchemy, pytest, slowapi. Python: `./.venv/Scripts/python.exe`.

**Branch:** `feat/guest-login-backend` off `main`. **Dir:** `cloud-backend/`.
**This is Plan A of 2** (B = PWA, depends on A).

---

### Task 0: Branch
- [ ] `git checkout -b feat/guest-login-backend`

---

### Task 1: `is_guest` on the User model + migration

**Files:** Modify `app/models/user.py`; Create `migrate_add_guest.py`.

- [ ] **Step 1:** In `app/models/user.py`, add after the `subscription_enabled` line:

```python
    is_guest = Column(Boolean, default=False, nullable=False)  # anonymous guest account (no wallet)
```

- [ ] **Step 2:** Create `migrate_add_guest.py` (prod ALTER; SQLite tests use create_all):

```python
"""Add users.is_guest column. Run manually (matches migrate_*.py convention):

    python migrate_add_guest.py
"""
from sqlalchemy import text

from app.db.session import engine


def run() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_guest BOOLEAN NOT NULL DEFAULT FALSE"))
        else:
            # SQLite: ADD COLUMN is idempotent only if missing; ignore "duplicate column".
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_guest BOOLEAN NOT NULL DEFAULT 0"))
            except Exception as e:  # noqa: BLE001
                if "duplicate column" not in str(e).lower():
                    raise
    print("is_guest column ensured.")


if __name__ == "__main__":
    run()
```

- [ ] **Step 3:** Commit:
```bash
git add app/models/user.py migrate_add_guest.py
git commit -m "feat(auth): add users.is_guest column + migration"
```

---

### Task 2: `get_non_guest_user` dependency (TDD)

**Files:** Modify `app/core/security.py`; Test `tests/test_security_guest.py`.

- [ ] **Step 1: Failing test** — create `tests/test_security_guest.py`:

```python
import pytest
from fastapi import HTTPException

from app.core.security import get_non_guest_user
from tests.conftest import make_user


def test_non_guest_user_passes(db):
    u = make_user(db, email="real@test.in")
    assert get_non_guest_user(current_user=u) is u


def test_guest_user_blocked(db):
    g = make_user(db, email="g@guest.printit", is_guest=True)
    with pytest.raises(HTTPException) as exc:
        get_non_guest_user(current_user=g)
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run → fails** (`ImportError: get_non_guest_user`):
```bash
./.venv/Scripts/python.exe -m pytest tests/test_security_guest.py -q
```

- [ ] **Step 3:** In `app/core/security.py`, add after `get_current_admin_user`:

```python
def get_non_guest_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if getattr(current_user, "is_guest", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature is not available for guest accounts.",
        )
    return current_user
```

- [ ] **Step 4: Run → passes:**
```bash
./.venv/Scripts/python.exe -m pytest tests/test_security_guest.py -q
```

> Note: `make_user` already forwards `**kw` to the `User` constructor (set up in a prior plan),
> so `make_user(db, is_guest=True)` works once Task 1's column exists.

- [ ] **Step 5: Commit:**
```bash
git add app/core/security.py tests/test_security_guest.py
git commit -m "feat(auth): add get_non_guest_user authorization dependency"
```

---

### Task 3: `POST /auth/guest` (TDD on the creation helper)

**Files:** Modify `app/routers/auth.py`; Test `tests/test_auth_guest.py`.

- [ ] **Step 1: Failing test** — create `tests/test_auth_guest.py`:

```python
from app.routers.auth import create_guest_user
from app.models.user import User


def test_create_guest_user(db):
    u = create_guest_user(db)
    assert u.id is not None
    assert u.is_guest is True
    assert u.is_admin is False
    assert u.is_kiosk_owner is False
    assert u.email.startswith("guest_") and u.email.endswith("@guest.printit")
    # password is unusable / random, full name labelled
    assert u.full_name == "Guest"
    # a second guest gets a distinct email
    u2 = create_guest_user(db)
    assert u2.email != u.email
    assert db.query(User).filter(User.is_guest == True).count() == 2  # noqa: E712
```

- [ ] **Step 2: Run → fails** (`ImportError: create_guest_user`):
```bash
./.venv/Scripts/python.exe -m pytest tests/test_auth_guest.py -q
```

- [ ] **Step 3:** In `app/routers/auth.py`, add the helper (near the other helpers, after
  `create_refresh_token`) and the endpoint. The helper:

```python
def create_guest_user(db: Session) -> User:
    """Create an anonymous guest account (no wallet access)."""
    email = f"guest_{secrets.token_hex(16)}@guest.printit"
    user = User(
        email=email,
        hashed_password=get_password_hash(secrets.token_urlsafe(32)),
        full_name="Guest",
        is_guest=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
```

  The endpoint (place near `/register`, mirrors `/login`'s token + cookie):

```python
@router.post("/guest")
@limiter.limit("5/minute")
def guest_login(request: Request, response: Response, db: Session = Depends(get_db)):
    user = create_guest_user(db)
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": str(user.id)}, expires_delta=access_token_expires)
    refresh_token = create_refresh_token(user.id, db)

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )
    return {"access_token": access_token, "token_type": "bearer", "is_guest": True}
```

> `Request`/`Response` are already imported in `auth.py`; `secrets`, `get_password_hash`,
> `create_access_token`, `create_refresh_token`, `limiter`, `settings`, `get_db` too.

- [ ] **Step 4: Run → passes:**
```bash
./.venv/Scripts/python.exe -m pytest tests/test_auth_guest.py -q
```

- [ ] **Step 5: Commit:**
```bash
git add app/routers/auth.py tests/test_auth_guest.py
git commit -m "feat(auth): POST /auth/guest creates rate-limited anonymous account"
```

---

### Task 4: Revoke wallet for guests (server-side)

**Files:** Modify `app/routers/wallet.py`; Test `tests/test_wallet_guest.py`.

- [ ] **Step 1: Failing test** — create `tests/test_wallet_guest.py`:

```python
import pytest
from fastapi import HTTPException

from app.routers.wallet import wallet_me
from tests.conftest import make_user


def test_wallet_me_blocks_guest(db):
    g = make_user(db, email="g2@guest.printit", is_guest=True)
    with pytest.raises(HTTPException) as exc:
        wallet_me(db=db, current_user=g)
    assert exc.value.status_code == 403


def test_wallet_me_allows_real_user(db):
    u = make_user(db, email="real2@test.in")
    out = wallet_me(db=db, current_user=u)
    assert "balance" in out
```

> These call `wallet_me` directly. After Step 3, `wallet_me`'s `current_user` default is
> `Depends(get_non_guest_user)` — but calling it directly bypasses Depends, so to make the
> guest-block assertion meaningful the test passes the guard explicitly. Replace the test
> body to call the guard wiring directly: see Step 4 note. (Keep the real-user test as-is.)

- [ ] **Step 2:** Change the wallet auth import. In `app/routers/wallet.py` replace:
```python
from app.core.security import get_current_user
```
with:
```python
from app.core.security import get_current_user, get_non_guest_user
```

- [ ] **Step 3:** Replace **all 7** `Depends(get_current_user)` occurrences in `wallet.py`
  with `Depends(get_non_guest_user)` (every wallet route — `wallet_me`, admin refund, admin
  bank-refund, `wallet_ledger`, topup order, hold, hold/multi). Leave the webhook endpoints
  (they use signature verification, not `get_current_user`).

- [ ] **Step 4:** Because direct calls bypass `Depends`, make the guest-block test assert the
  *dependency* (the real guard). Replace `tests/test_wallet_guest.py` with:

```python
import pytest
from fastapi import HTTPException

from app.core.security import get_non_guest_user
from app.routers.wallet import wallet_me
from tests.conftest import make_user


def test_wallet_guard_blocks_guest(db):
    g = make_user(db, email="g2@guest.printit", is_guest=True)
    with pytest.raises(HTTPException) as exc:
        get_non_guest_user(current_user=g)   # the dependency wallet routes now use
    assert exc.value.status_code == 403


def test_wallet_me_allows_real_user(db):
    u = make_user(db, email="real2@test.in")
    out = wallet_me(db=db, current_user=u)   # real user still works
    assert "balance" in out
```

- [ ] **Step 5: Verify the wiring + run suite:**
```bash
grep -c "Depends(get_non_guest_user)" app/routers/wallet.py    # expect 7
grep -c "Depends(get_current_user)" app/routers/wallet.py      # expect 0
./.venv/Scripts/python.exe -m pytest -q                        # all pass
```

- [ ] **Step 6: Commit:**
```bash
git add app/routers/wallet.py tests/test_wallet_guest.py
git commit -m "feat(auth): block guests from all wallet endpoints (403)"
```

---

### Task 5: Document the migration rollout
- [ ] In `cloud-backend/CLAUDE.md`, after the `migrate_add_indexes.py` bullet, add:
```markdown
- Guest accounts: `migrate_add_guest.py` adds `users.is_guest`. Run after deploy:
  `fly ssh console -C "python migrate_add_guest.py"`.
```
- [ ] Commit: `git add CLAUDE.md && git commit -m "docs: note guest-account migration rollout"`

---

## Self-review
- **Spec coverage:** §1 model+migration+endpoint (Tasks 1,3); §2/§3 `get_non_guest_user` +
  wallet 403 + rate-limit (Tasks 2,3,4); admin/owner exclusion is inherent (no task needed).
- **Security:** rate-limit 5/min on `/auth/guest`; wallet 403 server-side; random unusable
  password; guest has no roles; same token/cookie hardening as real users.
- **Type/name consistency:** `create_guest_user(db)`, `get_non_guest_user(current_user)`,
  `is_guest` used consistently across tasks and tests.
- **No placeholders.** Tests verify the security-critical guard + creation; wallet wiring
  verified by grep counts + full suite.
- **Prod rollout:** `migrate_add_guest.py` run via `fly ssh` (documented, Task 5).
