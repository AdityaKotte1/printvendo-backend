# PrintIT — Performance & Redundancy, Phase 1 + 1B (Design Spec)

**Date:** 2026-06-15
**Status:** Approved design, ready for implementation plan
**Scope owner:** Track A (performance + redundancy), infra-light

---

## 1. Background & goal

PrintIT is a 6-component IoT print-kiosk SaaS (FastAPI backend, Pi agent, end-user
Next.js PWA, vanilla admin dashboard, vanilla refiller app, Vite landing). A
full-system audit found that the reported slowness is **not one bug** — it is a small
set of anti-patterns repeated everywhere: missing DB indexes, N+1 query loops, a write
on a read endpoint, duplicated revenue logic, synchronous heavy work in the request
path, wasteful client polling, and refetch-on-every-navigation in the dashboards. A
parallel UX audit found the same redundancy in the front ends: duplicated client logic
(pricing, page-range, options, Razorpay, modals) in the PWA, and ~1000+ LOC of
copy-pasted shell code across the dashboard's 11 pages.

**Goal:** make the system noticeably faster and collapse the duplicated logic **without
new infrastructure and without changing user-visible behavior**, as a measurable first
increment. Heavier items (backgrounding Ghostscript, virtualization, bundle splitting)
and all behaviour-changing UX-flow changes are explicitly deferred.

### Decisions locked during brainstorming
- **Infra-light:** no Redis, no Celery/RQ, no PgBouncer. Use DB indexes, batch queries,
  in-process TTL cache, FastAPI `BackgroundTasks`/threadpool only if needed, and
  client-side caching.
- **Phase 1 quick wins first:** ship the high-impact/low-risk batch, measure, then
  continue.
- **No behaviour change** in Phase 1 / 1B. Every endpoint response shape and every
  screen's function stays identical. Behaviour-changing UX work is Phase 2 (separate
  spec).

---

## 2. Success criteria

1. Hot endpoints (`GET /printers/`, `/jobs/summary`, `/kiosk/*`, `/owner/*`) drop from
   O(rows) queries to a bounded handful, backed by indexes.
2. Revenue / commission / unsettled math exists in **exactly one** module; `owner.py`
   and `kiosk.py` produce **byte-identical** JSON to today (proven by characterization
   tests captured before the refactor).
3. `GET /printers/` performs **no DB write** on the read path.
4. PWA becomes interactive without blocking on `/admin/me`; notification polling only
   runs for admins and at a sane interval; per-job 1s timers collapse to one.
5. Dashboards stop refetching identical data (`/kiosk/printers`, `/kiosk/me`,
   `/subscriptions/plans`) on every navigation; 30s polling is visibility-gated and
   updates charts/sidebar in place.
6. Duplicated PWA client logic is consolidated into shared modules; duplicated dashboard
   shell code is consolidated into `app.js`.
7. Admin-nav visibility is verified server-side on every admin-nav page (security fix).
8. **No regression** in behaviour: characterization tests + manual smoke of the core
   flows (upload → pay → print, wallet top-up, settlements, subscribe) pass unchanged.

### How we measure
- Backend: lightweight request-timing logs on the targeted endpoints; record
  before/after for one representative account (printers list, jobs summary, owner/kiosk
  summary, revenue/by-day, invoice export).
- Frontend: Chrome DevTools — PWA cold-start time-to-interactive, and number of network
  requests per dashboard navigation, before vs after.
- No APM/observability infra is added.

---

## 3. Architecture & approach

No structural change to the system. Work is three concerns:

1. **Backend hot paths** — indexes, batch loads, remove write-on-read, pool sizing,
   small in-process TTL cache for read-only aggregations, and a single revenue/printer
   service that both `owner.py` and `kiosk.py` call.
2. **PWA** — extract duplicated logic into `lib/` modules + small components/hooks; make
   auth init non-blocking; fix polling/timers; reuse already-loaded state; batch the
   per-printer pricing fetch.
3. **Dashboards** — extract the repeated shell (sidebar, theme, admin check, printer
   loading, date presets, shared row/card renderers) into `app.js`; add a cached,
   deduped fetch wrapper; visibility-gate polling and update DOM/charts in place; verify
   admin nav server-side.

### New backend modules
- `app/services/revenue_service.py` — single source of truth:
  - `owned_printer_ids(db, user) -> list[int]`
  - `printer_stats(db, printer_ids, *, date_range=None) -> dict[int, dict]`
    (per-printer revenue, job counts, queue length, wallet-vs-razorpay split)
  - `owner_earnings(db, user_id, printer_ids) -> dict`
    (`gross, refunds, commission, commission_rate, net, owner_share, settled_and_pending,
    unsettled`) — the 0%/10% subscription rule lives here only.
- `app/core/cache.py` — tiny in-process TTL cache utility (dict + monotonic timestamp,
  per-worker). Used only to wrap read-only stats aggregations; never wraps money/job-
  state mutations. Default TTL 30–60s, keyed by (endpoint, user_id, params).
- `app/db/indexes.py` — idempotent index creation (`CREATE INDEX IF NOT EXISTS …`) run
  once at startup after `Base.metadata.create_all`. Works on Postgres and SQLite. No
  Alembic adoption.

### New PWA modules (no behaviour change)
- `lib/pricing.ts` — `calculateJobPrice(job, printerPricing?)`, the single client-side
  pricing function (handles centralized defaults, printer overrides, and paper-shop
  items). Replaces `jobs/new.tsx:55` and `printers.tsx:265`.
- `lib/pageRange.ts` — `parsePageRange(rangeStr, totalPages)`. Replaces `jobs/new.tsx:27`
  and `printers.tsx:171`.
- `lib/jobOptions.ts` — `parseJobOptions(...)` + `formatOptionPills(...)`; plus
  `components/OptionPills.tsx`. Replaces option parsing/pills in `index`, `printers`,
  `jobs/new`, `canvas`.
- `lib/jobDisplay.ts` — `formatJobState`, `formatJobAction`, `getJobChip` (moved out of
  `index.tsx`).
- `lib/useRazorpayCheckout.ts` — shared Razorpay open/handler hook + a singleton script
  loader. Replaces duplicated logic in `payment.tsx:185` and `wallet.tsx:137`.
- `lib/useBalanceAnimation.ts` — shared count-up hook (from `index.tsx`/`wallet.tsx`).
- `lib/useModalDialog.ts` + `components/ModalDialog.tsx` — one modal system replacing the
  ~200-line per-page boilerplate on 6+ pages.

> Note: the PWA is `next export` (static) Pages Router. New files go under existing
> `lib/` and a new `components/` dir; no routing or SSR changes.

### Dashboard shared code (into `app.js`)
- `populateSidebarPrinters(activeId?)`, `initTheme()`, `verifyAdminServerSide()`
  (calls `/kiosk/me`, caches result short-term), `applyDatePreset(...)` /
  `initDatePicker(...)`, `renderSettlementRow(...)`, `renderChangeRequestCard(...)`.
- `authFetchCached(path, {ttlMs})` + in-flight request dedupe, used for
  `/kiosk/printers`, `/kiosk/me`, `/subscriptions/plans`.
- A single sidebar markup source (JS-built) so the ~70–90 LOC `<aside>` is not pasted in
  all 10 pages.

---

## 4. Detailed scope (Phase 1 + 1B)

### 4A. Backend — database (highest ROI, lowest risk)
- **Indexes** (`app/db/indexes.py`, idempotent at startup):
  - `Payment`: `status`, `printer_id`, `user_id`, `job_id`, `razorpay_order_id`,
    composite `(status, created_at)`.
  - `PrinterJob`: `printer_id`, `job_id`, `status`, composite `(status, created_at)`.
  - `Job`: `user_id`. `WalletLedger`: `(user_id, status)`.
- **Connection pool** (`app/db/session.py`): `pool_size=20, max_overflow=10,
  pool_recycle=3600` (keep `pool_pre_ping=True`).
- **Kill N+1s** (batch-load maps before loops):
  - `owner.py` invoice export (`~662`), pending/history settlements (`~933`/`~968`),
    admin printer lists (`~1192`/`~1220`).
  - `printers.py` `GET /` per-printer `has_active_subscription` (`~185`) → prefetch all
    relevant owner subscriptions once.
  - `kiosk.py` refill logs (`~556`) → `joinedload(PaperRefillLog.refilled_by)`.

### 4B. Backend — remove write-on-read
- `printers.py` `GET /` (`~141–200`): derive displayed `OFFLINE` from heartbeat age in
  the response only; **remove the `printer.status = "OFFLINE"` commit** (line ~178).
  Authoritative status transitions remain owned by the heartbeat/health endpoints
  (`pi.py`, `printers.py` heartbeat). Verify no other reader depends on that write.

### 4C. Backend — collapse duplicated revenue logic
- Implement `revenue_service.py` (see §3).
- **Before refactor:** capture characterization tests snapshotting current JSON for:
  `/owner/summary`, `/owner/printers`, `/owner/revenue/{user_id}`,
  `/kiosk/summary`, `/kiosk/printers`, `/kiosk/settlements/unsettled`,
  `/owner/revenue/by-day`, `/kiosk/revenue/by-day` (representative inputs).
- Refactor `owner.py` and `kiosk.py` to call the service. Tests must stay green
  (byte-identical output).

### 4D. Backend — light caching on read-only aggregations
- Wrap `/owner/summary`, `/kiosk/summary`, and the `revenue/by-day` aggregations with
  `app/core/cache.py` (TTL 30–60s, keyed by user + params). Absorbs the 30s dashboard
  refresh storm. Never applied to mutation or money/job-state paths.

### 4E. PWA — quick wins (no behaviour change)
- `lib/AuthContext.tsx` (`~33–44`): seed `isAdmin` optimistically from localStorage;
  verify `/admin/me` in the background; do not block app init on it.
- `pages/index.tsx`: notification-count polling guarded to `isAdmin`, interval 30s
  (was 10s, all users) (`~213`); collapse per-job 1s `JobCountdown` timers (`~36–88`)
  into one shared 60s ticker.
- `pages/_app.tsx` (`~407`): SW update check 10min → 1h.
- `pages/printers.tsx`: reuse the job already loaded on home (pass via router/context,
  fall back to fetch) (`~133`); fetch printer pricing in parallel instead of per-tap
  sequential (`~450`); memoize the haversine distance map (`~350–435`).
- Extract shared modules per §3 (`pricing`, `pageRange`, `jobOptions`+`OptionPills`,
  `jobDisplay`, `useRazorpayCheckout`+singleton loader, `useBalanceAnimation`,
  `useModalDialog`+`ModalDialog`) and replace the duplicated in-page implementations.

### 4F. Dashboards — quick wins + DRY (no behaviour change)
- `app.js`: add `authFetchCached` (sessionStorage TTL + in-flight dedupe); cache
  `/kiosk/printers`, `/kiosk/me`, `/subscriptions/plans`.
- Extract shared shell into `app.js` (sidebar, theme, admin check, printer loading, date
  presets, settlement-row + change-request-card renderers); replace per-page copies.
- Polling: visibility-gated, 30s → 60s; update Chart.js **in place** (no destroy/create);
  diff sidebar before rebuilding DOM (`dashboard.html:537`, `printer-detail.html:828`,
  refiller `dashboard.html:320`).

### 4H. Phase 1B.1 — New-print flow redesign (PWA `pages/jobs/new.tsx`)
Behaviour-improving redesign of the upload + options experience. Approved approach:
**client-side pdf.js page counting, single upload, no backend change.**

- **Remove the double upload + manual analyze step.** Today the file is POSTed to
  `/jobs/analyze` (`new.tsx:201`) *and again* to `/jobs/` (`new.tsx:258`), and options
  are gated behind a per-file "Analyze" button (`new.tsx:377`). New flow:
  - On file add, read `numPages` in-browser via the already-bundled `pdfjs-dist`
    (worker at `public/pdf.worker.mjs`). No analyze upload.
  - Options render immediately; price computed locally via `lib/pricing.ts` +
    `lib/pageRange.ts` (the Phase 1B shared modules).
  - Single CTA uploads each file exactly once to `POST /jobs/` (returns authoritative
    page_count + price), then routes to `/printers?jobIds=…`.
  - **Fallback:** if pdf.js cannot parse a file (encrypted/corrupt), show "pages
    calculated at upload", keep the file enabled, and let the single `/jobs/` upload
    validate. No regression vs today.
  - `/jobs/analyze` endpoint is left intact for other callers; this flow stops using it.
- **Redesign the options UI** (mobile-first, app CSS tokens, lucide icons, no emoji):
  - **Copies:** stepper (− / value / +), 44px targets, tabular figures, range 1–50.
  - **Color:** segmented control `[ B/W | Color ]` (selected = accent fill).
  - **Sides:** segmented control `[ Single | Double ]`.
  - **Pages:** `[ All pages | Custom ]` toggle; Custom reveals the range input with
    helper text `e.g. 1-3, 5, 8-10`, inline validation on blur, and an effective-page
    hint.
  - Live per-file price breakdown + total; single primary CTA "Upload & continue".
  - Replace the inline-style soup with token-based classes; 150–300ms transitions that
    respect `prefers-reduced-motion`; meets touch-target and contrast rules from the
    UI/UX pass.
- **No behaviour change to pricing or job semantics** — only *when/where* page count is
  obtained (client vs an extra upload) and the visual/interaction design.

### 4G. Security fix (no behaviour change for legit users)
- Replace client-only `sessionStorage.is_admin` admin-nav gating with
  `verifyAdminServerSide()` (`/kiosk/me`) on every admin-nav page
  (`settlements.html:124`, `printer-detail.html:280`, `payment-config.html:137`,
  `subscription-history.html:160`, plus any other page exposing admin nav). Note: this
  hides UI only — server endpoints already enforce `is_admin`; this removes a DevTools
  spoof of the *nav*, not an actual privilege escalation.

---

## 5. Out of scope (deferred)

**Phase 2 — full Track A perf (later spec):** background Ghostscript normalization off
the request thread; list/grid virtualization (jobs, paper shop, job history tables);
pdfjs bundle splitting / on-demand page render; pagination overhauls; any Redis/worker.

**Phase 2 — UX-flow changes (separate brainstorm each, behaviour-changing):**
1. Merge the PWA two-modal payment flow into one; fix step-counter inconsistency.
2. Unify owner/admin dashboard page pairs (`settlements`↔`admin-settlements`,
   `payment-config`↔`admin-payment-config`) into role-aware pages.
3. Reconcile owner/admin tools duplicated between the PWA (`profile.tsx`,
   `owner/settlements.tsx`) and the dashboard; pick a canonical home.

**Other tracks (later):** new features (Track D), large-module refactors beyond the
revenue service (Track C), per-user notifications, the token-expiry/heartbeat-TTL bug
fixes (Track B) — unless trivially co-located with Phase 1 work.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Revenue refactor changes output | Characterization tests captured first; must stay byte-identical |
| Index creation briefly locks tables | Small dataset; run at low traffic; `IF NOT EXISTS` idempotent; documented |
| In-process cache staleness | Short TTL (30–60s); only on read-only stats; never money/job-state |
| Per-worker cache divergence (2 workers) | Acceptable for dashboard stats; not used for correctness-critical reads |
| Removing write-on-read breaks a consumer | Audit all readers of `printer.status`; heartbeat/health still write it |
| Frontend extraction introduces subtle diffs | Replace one duplication at a time; smoke-test each flow; no logic change |
| PWA is static export | New files only under `lib/`/`components/`; no routing/SSR change |

---

## 7. Repository note

The project root is **not** a git repository; `cloud-backend/`, `pi-agent/`, and
`printit-web-app-for_end_user/` are nested repos, while the dashboards deploy from their
own roots. This spec lives at the project root under `docs/superpowers/specs/`. Decide
during planning whether to (a) `git init` the root to track cross-cutting docs, or
(b) commit each component's changes within its own repo. No commit is made by writing
this file.

---

## 8. Implementation sequencing (for the plan)

1. **DB indexes + pool sizing** — isolated, immediate, measurable.
2. **Remove write-on-read** in `GET /printers/`.
3. **Characterization tests** for revenue endpoints, then **extract `revenue_service`**
   and refactor `owner.py`/`kiosk.py`.
4. **Batch-load N+1s**.
5. **In-process TTL cache** on aggregations.
6. **PWA**: shared modules + auth-init + polling/timer fixes + printers.tsx reuse/batch;
   then **Phase 1B.1** new-print flow redesign (client-side pdf.js, single upload,
   options UI) on top of the shared `pricing`/`pageRange` modules.
7. **Dashboards**: `authFetchCached` + shell extraction into `app.js` + polling/chart/
   sidebar fixes + server-side admin verification.
8. **Measure** before/after; record results; hand off remaining items to Phase 2.
