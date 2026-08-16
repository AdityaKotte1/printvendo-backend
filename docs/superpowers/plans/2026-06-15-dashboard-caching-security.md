# Dashboard Caching + Security Foundation (Phase 1B) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Add a shared caching/dedupe + server-side admin-verification layer to the admin
dashboard's `app.js`, and cut the dashboard's refetch + polling waste — safely, since this
vanilla multi-page app has no build/test step.

**Architecture:** `app.js` gains **additive** helpers (`authFetchCached`, `invalidateCache`,
`verifyAdminServerSide`) backed by `sessionStorage` so a cached read survives navigation
between pages. Existing `authFetch` is untouched (zero risk to the 10 other pages).
`dashboard.html` adopts `authFetchCached` for low-volatility reads and switches its 30s
full-refresh to a visibility-gated 60s tick.

**Verification:** `node --check app.js` (real). `dashboard.html` inline-script edits are
verified by reading (no browser harness exists). **Broader rollout** (other pages adopting
`authFetchCached`/`verifyAdminServerSide`, sidebar/theme extraction) is a documented
follow-up that needs a browser smoke test — out of scope here to avoid unverifiable churn.

**Branch:** `perf/dashboard-caching` off `main`. **Working dir:** `printit-admin-dashboard/`.

---

### Task 0: Branch
- [ ] `git checkout -b perf/dashboard-caching`

---

### Task 1: Additive caching + admin-verify helpers in `app.js`

**Files:** Modify `printit-admin-dashboard/app.js` (append before the final line; do not change `authFetch`).

- [ ] **Step 1:** Append this block to the end of `app.js`:

```javascript
// =============================================
//  CACHED READS (speeds up navigation; survives page loads via sessionStorage)
// =============================================

const _afcInflight = {};

function _afcRead(path, ttlMs) {
  try {
    const raw = sessionStorage.getItem('afc_' + path);
    if (!raw) return undefined;
    const { t, v } = JSON.parse(raw);
    if (Date.now() - t < ttlMs) return v;
  } catch { /* ignore */ }
  return undefined;
}

/**
 * Cached GET wrapper around authFetch for low-volatility reads.
 * Returns a cached value if younger than ttlMs; de-dupes concurrent calls.
 * Only use for reads that tolerate brief staleness (printers list, profile, plans).
 */
async function authFetchCached(path, { ttlMs = 10000 } = {}) {
  const hit = _afcRead(path, ttlMs);
  if (hit !== undefined) return hit;
  if (_afcInflight[path]) return _afcInflight[path];
  const p = authFetch(path)
    .then((v) => {
      try { sessionStorage.setItem('afc_' + path, JSON.stringify({ t: Date.now(), v })); } catch { /* ignore */ }
      delete _afcInflight[path];
      return v;
    })
    .catch((e) => { delete _afcInflight[path]; throw e; });
  _afcInflight[path] = p;
  return p;
}

/** Drop a cached entry (call after a mutation) or all entries when no path given. */
function invalidateCache(path) {
  try {
    if (path) { sessionStorage.removeItem('afc_' + path); return; }
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const k = sessionStorage.key(i);
      if (k && k.startsWith('afc_')) sessionStorage.removeItem(k);
    }
  } catch { /* ignore */ }
}

/**
 * Server-verified admin check (not the spoofable client sessionStorage flag).
 * Pages that reveal admin-only navigation should gate on this.
 */
async function verifyAdminServerSide() {
  try {
    const me = await authFetchCached('/kiosk/me', { ttlMs: 60000 });
    return !!(me && me.is_admin);
  } catch { return false; }
}
```

- [ ] **Step 2: Syntax check**

```bash
node --check app.js
```

Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add app.js
git commit -m "feat(dashboard): add cached reads + server-side admin verify helpers"
```

---

### Task 2: Adopt caching + tame polling in `dashboard.html`

**Files:** Modify `printit-admin-dashboard/dashboard.html`.

- [ ] **Step 1:** Change the printers fetch (line ~233) from:

```javascript
        authFetch('/kiosk/printers'),
```

to:

```javascript
        authFetchCached('/kiosk/printers', { ttlMs: 8000 }),
```

- [ ] **Step 2:** Change the plans fetch (line ~664) from:

```javascript
        const plans = await authFetch('/subscriptions/plans');
```

to:

```javascript
        const plans = await authFetchCached('/subscriptions/plans', { ttlMs: 300000 });
```

- [ ] **Step 3:** Change the auto-refresh (line ~537) from:

```javascript
  setInterval(loadDashboard, 30000);
```

to (visibility-gated, 60s; bypass stale printer cache on each tick):

```javascript
  setInterval(() => {
    if (document.hidden) return;
    invalidateCache('/kiosk/printers');
    loadDashboard();
  }, 60000);
```

- [ ] **Step 4: Verify by reading** the three edits are syntactically intact (matched
  parens/braces, comma still valid inside the `Promise.all([...])` array for Step 1).

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "perf(dashboard): cache printers/plans + visibility-gated 60s refresh"
```

---

## Follow-up (documented, needs browser verification — not in this plan)
- Switch the remaining pages' `/kiosk/printers`, `/kiosk/me`, `/subscriptions/plans` reads
  to `authFetchCached` (cross-page navigation win).
- Replace client-only `sessionStorage.kiosk_user.is_admin` admin-nav gating with
  `verifyAdminServerSide()` on `settlements.html`, `printer-detail.html`,
  `payment-config.html`, `subscription-history.html` (security).
- Extract the duplicated sidebar/theme/date-preset markup into shared `app.js` helpers.
- Refiller app: visibility-gate its 30s printer poll.

## Self-review notes
- **Safe by construction:** `app.js` additions don't touch `authFetch`; only `dashboard.html`
  changes behaviour, and the 60s tick invalidates the printers cache so refresh stays fresh.
- **Spec coverage:** lays the §4F caching foundation + the §4G security helper; full rollout
  is the documented follow-up (honest about the no-browser-test constraint).
- **Verification:** `node --check app.js`; dashboard edits read-verified.
