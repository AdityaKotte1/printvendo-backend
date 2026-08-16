# PWA Quick Wins (Phase 1B) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Remove startup blocking and wasteful polling in the PWA — no behaviour change.

**Architecture:** (1) AuthContext stops blocking app init on `/admin/me` (optimistic
`isAdmin` from localStorage, verify in background). (2) Notification-count polling only
runs for admins, at 30s not 10s. (3) Per-job 1s countdown timers collapse to one shared
module ticker. (4) Service-worker update check 10min → 60min.

**Tech Stack:** Next.js 14 PWA, TypeScript. Verify with `npx tsc --noEmit` + `npm run build`.

**Branch:** `perf/pwa-quick-wins` off `main`. **Working dir:** `printit-web-app-for_end_user/`.

---

### Task 0: Branch
- [ ] `git checkout -b perf/pwa-quick-wins`

---

### Task 1: Non-blocking auth init (`lib/AuthContext.tsx`)

- [ ] **Step 1:** Replace the init `useEffect` (lines 24-48) with:

```tsx
  useEffect(() => {
    (async () => {
      const { token: storedToken, email: storedEmail, fullName: storedFullName } = await loadToken();
      if (storedEmail) setEmail(storedEmail);
      if (storedFullName) setFullName(storedFullName);

      if (storedToken) {
        // Optimistic: unblock the app immediately with cached state.
        setToken(storedToken);
        try {
          const cached = typeof window !== 'undefined' ? window.localStorage.getItem('printit_isAdmin') : null;
          if (cached != null) setIsAdmin(cached === '1');
        } catch { /* ignore */ }
      }
      setInitializing(false);

      // Verify the token in the background; reconcile without blocking startup.
      if (storedToken) {
        try {
          const admin = await checkIsAdmin(storedToken);
          setIsAdmin(admin);
          try { window.localStorage.setItem('printit_isAdmin', admin ? '1' : '0'); } catch { /* ignore */ }
        } catch (error) {
          await clearToken();
          await apiLogout();
          setToken(null);
          setIsAdmin(false);
          try { window.localStorage.removeItem('printit_isAdmin'); } catch { /* ignore */ }
        }
      }
    })();
  }, []);
```

- [ ] **Step 2:** In `setSession` (after `setIsAdmin(admin);`, line ~58) add caching:

```tsx
    try { window.localStorage.setItem('printit_isAdmin', admin ? '1' : '0'); } catch { /* ignore */ }
```

- [ ] **Step 3:** In `logout` (after `setIsAdmin(false);`, line ~68) add:

```tsx
    try { window.localStorage.removeItem('printit_isAdmin'); } catch { /* ignore */ }
```

- [ ] **Step 4:** `npx tsc --noEmit` → no errors. Commit:
```bash
git add lib/AuthContext.tsx
git commit -m "perf(pwa): non-blocking auth init with cached isAdmin"
```

---

### Task 2: Service-worker update interval (`pages/_app.tsx`)

- [ ] **Step 1:** Replace line 418-419:

```tsx
    // Check for updates every 10 minutes
    const interval = setInterval(checkUpdates, 10 * 60 * 1000);
```

with:

```tsx
    // Check for updates every 60 minutes
    const interval = setInterval(checkUpdates, 60 * 60 * 1000);
```

- [ ] **Step 2:** Commit:
```bash
git add pages/_app.tsx
git commit -m "perf(pwa): SW update check every 60min (was 10min)"
```

---

### Task 3: Notification polling only for admins + 30s (`pages/index.tsx`)

- [ ] **Step 1:** Replace the polling effect (lines 227-233):

```tsx
  useEffect(() => {
    void fetchNotificationCount();
    const interval = setInterval(() => {
      void fetchNotificationCount();
    }, 10000);
    return () => clearInterval(interval);
  }, [fetchNotificationCount]);
```

with (no interval created for non-admins; 30s for admins):

```tsx
  useEffect(() => {
    if (!token || !isAdmin) return;
    void fetchNotificationCount();
    const interval = setInterval(() => {
      void fetchNotificationCount();
    }, 30000);
    return () => clearInterval(interval);
  }, [token, isAdmin, fetchNotificationCount]);
```

- [ ] **Step 2:** Commit:
```bash
git add pages/index.tsx
git commit -m "perf(pwa): poll notifications only for admins at 30s"
```

---

### Task 4: One shared countdown ticker (`pages/index.tsx`)

- [ ] **Step 1:** Add a module-level ticker above `const JobCountdown` (before line 36):

```tsx
// One shared 1s ticker for all countdowns (instead of one setInterval per job).
const _tickSubs = new Set<() => void>();
let _tickTimer: ReturnType<typeof setInterval> | null = null;
function subscribeTick(cb: () => void): () => void {
  _tickSubs.add(cb);
  if (_tickTimer == null) {
    _tickTimer = setInterval(() => { _tickSubs.forEach((fn) => fn()); }, 1000);
  }
  return () => {
    _tickSubs.delete(cb);
    if (_tickSubs.size === 0 && _tickTimer != null) {
      clearInterval(_tickTimer);
      _tickTimer = null;
    }
  };
}
```

- [ ] **Step 2:** Replace the `JobCountdown` body's state + effect (lines 37-58) with a
  derived-on-tick version. Replace:

```tsx
  const [timeLeft, setTimeLeft] = useState<number>(0);
  const EXPIRY_MS = 24 * 60 * 60 * 1000;

  useEffect(() => {
    const calculate = () => {
      const created = new Date(createdAt).getTime();
      const diff = created + EXPIRY_MS - Date.now();
      if (diff <= 0) {
        setTimeLeft(0);
        onExpire(jobId);
        return false;
      }
      setTimeLeft(diff);
      return true;
    };

    calculate();
    const timer = setInterval(() => {
      if (!calculate()) clearInterval(timer);
    }, 1000);
    return () => clearInterval(timer);
  }, [createdAt, jobId, onExpire]);
```

with:

```tsx
  const EXPIRY_MS = 24 * 60 * 60 * 1000;
  const [, force] = useReducer((x: number) => x + 1, 0);
  const firedRef = useRef(false);

  useEffect(() => subscribeTick(force), []);

  const timeLeft = Math.max(0, new Date(createdAt).getTime() + EXPIRY_MS - Date.now());

  useEffect(() => {
    if (timeLeft <= 0 && !firedRef.current) {
      firedRef.current = true;
      onExpire(jobId);
    }
  }, [timeLeft, jobId, onExpire]);
```

- [ ] **Step 3:** Ensure `useReducer` and `useRef` are imported at the top of `index.tsx`.
  Check the existing `import React, { ... } from 'react';` line and add `useReducer` and
  `useRef` if missing.

- [ ] **Step 4:** `npx tsc --noEmit` → no errors; `npm run build` → succeeds. Commit:
```bash
git add pages/index.tsx
git commit -m "perf(pwa): consolidate per-job countdowns into one shared ticker"
```

---

## Self-review notes
- **Spec coverage:** implements §4E auth-init + polling/timer items. (printers.tsx reuse/
  batch/distance + Razorpay singleton deferred to a follow-up PWA plan — they touch larger
  surfaces and the printer pricing path.)
- **No behaviour change:** auth still validates token (now in background); countdown still
  ticks every 1s and still shows h/m/s; notifications still admin-only.
- **Verification:** `tsc --noEmit` + `next build` (no test runner in this repo).
