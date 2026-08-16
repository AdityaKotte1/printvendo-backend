# Guest Login — Design Spec

**Date:** 2026-06-15 · **Status:** Approved design, ready for plan

## Goal
Let end users start printing **without Google/email sign-in** via a "Continue as guest"
option. Guests can do everything a normal user can **except wallet** (top-up + pay-with-
wallet) and **invoices/receipts**, which are hidden. Guests pay via Razorpay only.
Security is enforced server-side, not just hidden in the UI.

## Decisions (locked)
- Guest = a real but **anonymous backend account** (`is_guest=true`), required so jobs /
  Razorpay payments work with a real `user_id`.
- Hidden for guests: **all wallet surfaces + invoices**. Everything else unchanged.
- **No** guest→real-account upgrade in v1.

---

## 1. Backend — guest identity & auth (`cloud-backend`)
- **Model:** add `is_guest = Column(Boolean, default=False, nullable=False)` to `User`.
  Migration script `migrate_add_guest.py` (`ALTER TABLE users ADD COLUMN is_guest ...`,
  idempotent / guarded), matching the project's manual-migration convention; documented in
  `CLAUDE.md` alongside the index migration.
- **`POST /auth/guest`** (in `auth.py`):
  - Rate-limited `@limiter.limit("5/minute")` (same posture as `/register`).
  - Creates `User(email=f"guest_{uuid4().hex}@guest.printit", hashed_password=
    get_password_hash(secrets.token_urlsafe(32)), full_name="Guest", is_guest=True)`.
  - Issues an access token (`{"sub": str(user.id)}`, standard expiry) **and** the refresh
    cookie, identical to `/login` (httpOnly, secure, samesite=lax).
  - Returns `{"access_token", "token_type": "bearer", "is_guest": true}`.

## 2. Authorization — what's revoked, enforced server-side
- **New dependency `get_non_guest_user`** in `app/core/security.py`: wraps
  `get_current_user`; raises `403 Forbidden` ("Wallet is not available for guest accounts")
  when `user.is_guest`. Applied to **every** `/wallet/*` route (replace their
  `get_current_user` dependency). This is the real enforcement — a hand-crafted request
  with a guest token still gets 403.
- **Admin/owner/kiosk/subscription endpoints** already require `is_admin` /
  `is_kiosk_owner` / `subscription_enabled`, all `False` for guests → guests are excluded
  automatically; **no change needed** (verified against `get_current_admin_user`,
  `get_owner_user`, `get_kiosk_user`).
- **Per-user data scoping** is unchanged: jobs/payments/invoices endpoints already filter
  by `current_user.id`, so a guest can only ever see its own data.

## 3. Security aspects (explicit — "everything secure")
| Concern | Mitigation |
|---|---|
| **Guest-account spam / DB bloat** | `/auth/guest` rate-limited 5/min per IP (slowapi). |
| **Unauthorized wallet use** | `get_non_guest_user` → 403 on all `/wallet/*` (server-side, not UI-only). |
| **Privilege escalation** | Guest created with `is_admin=False, is_kiosk_owner=False, subscription_enabled=False`; role-gated endpoints already reject them. |
| **Guest login as someone else** | Random `token_urlsafe(32)` password is never returned and unusable via `/login`; guest authenticates only via its issued JWT + DB-backed refresh token. |
| **Cross-user data access** | Existing `current_user.id` filters unchanged — guest sees only its own jobs/payments. |
| **Token theft window** | Same short-lived access token + httpOnly/secure/samesite refresh cookie + DB refresh-token revocation as real users — no weaker handling for guests. |
| **CORS** | No new origins; `/auth/guest` served from existing API origin already in the allowlist. |
| **Stale guest accumulation** | Out of scope for v1 (rate-limit is the guard). Documented follow-up: a cleanup job for guest accounts with no jobs older than N days. |

## 4. PWA (`printit-web-app-for_end_user`)
- **`lib/api.ts`:** add `guestLogin()` → `POST /auth/guest`, returns `{access_token, is_guest}`.
- **`lib/AuthContext.tsx`:** add `isGuest: boolean` to the context; `setSession` accepts/stores
  it; cache in localStorage (`printit_isGuest`, mirroring the `isAdmin` cache) and seed it on
  init. Cleared on logout.
- **`login.tsx`:** "Continue as guest" button (below Google) → `guestLogin()` →
  `setSession(token, isGuest=true)` → redirect `/`.
- **Hide for guests (keyed off `isGuest`):**
  - `index.tsx` — wallet card + "Add money".
  - `printers.tsx` — the "Pay with Wallet" button on the Confirm-&-Pay sheet (guests see only
    "Pay with Razorpay").
  - `profile.tsx` — wallet link **and** the invoices list/links.
  - **Route guards:** `/wallet` and `/invoices/[paymentId]` redirect guests to `/`.
- No change to real-user behavior.

## 5. Testing
- **Backend (pytest):** `/auth/guest` creates an `is_guest` user and returns a token; a
  `/wallet/*` endpoint raises 403 for a guest user; a non-guest still succeeds. (Suite ~17→~20.)
- **PWA:** `tsc --noEmit` + `npm run build`; gating verified by reading + manual browser check
  (no runtime test harness). Smoke: guest login → upload → Razorpay pay → no wallet/invoice UI.

## 6. Scope split
Two plans: **(A) backend** (model + migration + `/auth/guest` + `get_non_guest_user` + wallet
guard + tests), then **(B) PWA** (api + AuthContext + login button + gating/guards). A is the
dependency for B.

## Out of scope
Guest→real upgrade/merge; guest-account cleanup job; any wallet change for real users.
