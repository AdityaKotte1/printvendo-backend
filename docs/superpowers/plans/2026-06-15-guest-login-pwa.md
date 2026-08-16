# Guest Login — PWA (Plan B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Add a "Continue as guest" login and hide wallet + invoices for guests, keying off a
new `isGuest` flag in `AuthContext`.

**Architecture:** `guestLogin()` calls `POST /auth/guest`; `AuthContext` gains `isGuest`
(cached in localStorage like `isAdmin`). Pages read `useAuth().isGuest` to hide the wallet
card, the "Pay with Wallet" button, the profile wallet link + invoices, and to route-guard
`/wallet` and `/invoices/[paymentId]`. Server already enforces wallet 403 (Plan A).

**Tech Stack:** Next.js 14 PWA, TS. Verify: `npx tsc --noEmit` + `npm run build` + manual check.

**Branch:** `feat/guest-login-pwa` off `main`. **Dir:** `printit-web-app-for_end_user/`.
**Depends on Plan A** (backend `/auth/guest` + wallet 403).

---

### Task 0: Branch
- [ ] `git checkout -b feat/guest-login-pwa`

---

### Task 1: `guestLogin()` in `lib/api.ts`
- [ ] Add (e.g. right after the `login` function):

```typescript
export async function guestLogin(): Promise<{ access_token: string; is_guest: boolean }> {
  const res = await fetch(`${BASE_URL}/auth/guest`, { method: 'POST', credentials: 'include' });
  if (!res.ok) throw new Error('Could not start guest session. Please try again.');
  return res.json();
}
```

- [ ] Commit: `git add lib/api.ts && git commit -m "feat(pwa): guestLogin() api call"`

---

### Task 2: `isGuest` in `AuthContext`
**Files:** Modify `lib/AuthContext.tsx`.

- [ ] **Step 1:** Add `isGuest: boolean;` to `AuthContextValue` and extend `setSession`:

```tsx
  isAdmin: boolean;
  isGuest: boolean;
  initializing: boolean;
  setSession: (token: string, email: string, isAdmin: boolean, fullName?: string | null, isGuest?: boolean) => Promise<void>;
```

- [ ] **Step 2:** Add state: after `const [isAdmin, setIsAdmin] = useState(false);` add
  `const [isGuest, setIsGuest] = useState(false);`

- [ ] **Step 3:** Seed `isGuest` on init — inside the `if (storedToken) {` optimistic block,
  after the isAdmin cache read, add:

```tsx
        try {
          const cachedGuest = typeof window !== 'undefined' ? window.localStorage.getItem('printit_isGuest') : null;
          if (cachedGuest != null) setIsGuest(cachedGuest === '1');
        } catch { /* ignore */ }
```

- [ ] **Step 4:** Update `setSession` signature + body:

```tsx
  const setSession = async (
    newToken: string,
    newEmail: string,
    admin: boolean,
    newFullName?: string | null,
    guest: boolean = false,
  ) => {
    setToken(newToken);
    setEmail(newEmail);
    setIsAdmin(admin);
    setIsGuest(guest);
    try { window.localStorage.setItem('printit_isAdmin', admin ? '1' : '0'); } catch { /* ignore */ }
    try { window.localStorage.setItem('printit_isGuest', guest ? '1' : '0'); } catch { /* ignore */ }
    if (newFullName != null) {
      setFullName(newFullName);
    }
    await saveToken(newToken, newEmail, newFullName);
  };
```

- [ ] **Step 5:** In `logout`, after `setIsAdmin(false);` add `setIsGuest(false);` and after the
  `printit_isAdmin` remove add:
  `try { window.localStorage.removeItem('printit_isGuest'); } catch { /* ignore */ }`

- [ ] **Step 6:** Add `isGuest` to the provider value:
  `value={{ token, email, fullName, isAdmin, isGuest, initializing, setSession, logout }}`

- [ ] **Step 7:** `npx tsc --noEmit` → 0 errors. Commit:
  `git add lib/AuthContext.tsx && git commit -m "feat(pwa): track isGuest in AuthContext"`

---

### Task 3: "Continue as guest" button on `login.tsx`
**Files:** Modify `pages/login.tsx`.

- [ ] **Step 1:** Add `guestLogin` to the api import: change
  `import { checkIsAdmin, login, googleLogin } from '../lib/api';` to
  `import { checkIsAdmin, login, googleLogin, guestLogin } from '../lib/api';`

- [ ] **Step 2:** Add a handler near `handleSubmit`:

```tsx
  const handleGuest = async () => {
    setError(null);
    setLoading(true);
    try {
      const data = await guestLogin();
      await setSession(data.access_token, 'Guest', false, 'Guest', true);
      router.replace('/').catch(() => {});
    } catch (err: any) {
      setError(err?.message ?? 'Could not start guest session');
    } finally {
      setLoading(false);
    }
  };
```

- [ ] **Step 3:** Add a "Continue as guest" button directly below the Google sign-in block
  (read the JSX around the Google button container to anchor). Use the existing secondary
  button style; example:

```tsx
              <button
                type="button"
                onClick={handleGuest}
                disabled={loading || googleLoading}
                style={{ width: '100%', marginTop: 12, padding: '12px 16px', background: 'transparent', color: 'var(--text-muted)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md, 4px)', fontWeight: 600, cursor: 'pointer', opacity: (loading || googleLoading) ? 0.6 : 1 }}
              >
                Continue as guest
              </button>
```

- [ ] **Step 4:** `npx tsc --noEmit`. Commit:
  `git add pages/login.tsx && git commit -m "feat(pwa): continue-as-guest button"`

---

### Task 4: Hide wallet + invoices for guests
For each file: add `isGuest` to the `useAuth()` destructure, then apply the gating. Read each
anchor before editing.

- [ ] **`pages/index.tsx`** — destructure `isGuest`; wrap the wallet card button block (the
  `<button … onClick={() => router.push('/wallet')…}>` containing the balance hero, ~line 1103)
  in `{!isGuest && ( … )}`. Do the same for the secondary "Add Money" wallet push (~line 1524).

- [ ] **`pages/printers.tsx`** — destructure `isGuest`; wrap the **"Pay with Wallet"** button on
  the Confirm-&-Pay sheet in `{!isGuest && ( … )}` so guests see only "Pay with Razorpay".

- [ ] **`pages/profile.tsx`** — destructure `isGuest`; wrap the wallet link **and** the invoices
  list/links section in `{!isGuest && ( … )}`.

- [ ] **`pages/wallet.tsx`** — route guard. After the `useAuth()`/`useRouter()` calls add:

```tsx
  useEffect(() => {
    if (isGuest) router.replace('/').catch(() => {});
  }, [isGuest]);
```
  (Ensure `isGuest` is destructured from `useAuth()` and `useEffect` is imported.)

- [ ] **`pages/invoices/[paymentId].tsx`** — same route guard as wallet.tsx.

- [ ] **Verify:** `npx tsc --noEmit` (0 errors) + `npm run build` (compiles, all pages emitted).

- [ ] **Commit** (then revert generated artifacts):
```bash
git add pages/index.tsx pages/printers.tsx pages/profile.tsx pages/wallet.tsx "pages/invoices/[paymentId].tsx"
git commit -m "feat(pwa): hide wallet + invoices for guest users"
git checkout -- out public/sw.js public/.well-known/assetlinks.json 2>/dev/null; git clean -fd out >/dev/null 2>&1; rm -f tsconfig.tsbuildinfo
```

---

## Manual browser smoke test (before deploy)
`npm run dev` → "Continue as guest" → home shows **no wallet card**; upload → printer →
Confirm-&-Pay shows **only Razorpay** (no Pay-with-Wallet); profile shows **no wallet/invoices**;
visiting `/wallet` or `/invoices/x` redirects home; Razorpay pay still works.

## Self-review
- **Spec coverage:** §4 api (Task 1), AuthContext isGuest (Task 2), login button (Task 3),
  hide wallet card / pay-with-wallet / profile wallet+invoices + route guards (Task 4).
- **Type consistency:** `setSession(token,email,isAdmin,fullName?,isGuest?)`; `isGuest` added to
  `AuthContextValue` and provider value and every `useAuth()` consumer touched.
- **Security note:** UI hiding is convenience; the real wallet block is server-side (Plan A 403).
- **Verification:** tsc + build + manual smoke (no runtime test harness).
