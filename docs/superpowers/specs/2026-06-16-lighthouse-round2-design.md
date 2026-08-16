# Lighthouse Round 2 — Design Spec

**Date:** 2026-06-16 · **Status:** Approved approach, ready for plans

## Goal
Address the remaining real Lighthouse findings on `app.innvera.online` (Perf 81 / A11y 81):
the 0.245 CLS, 3.1 s LCP, residual color-contrast failures, ~12 KiB legacy JS, and the
`403 /admin/me` console error. (Round 1 already fixed lang, meta-description, font
preconnect, COOP, and the muted-text contrast bump.)

Out of scope (your decisions): viewport zoom stays disabled; brand accent button contrast
left as-is; CSP `unsafe-inline`/Trusted Types (inherent to static export). Ignore the
`chrome-extension://…/sdscript.js` JS audits (tester's Signer.Digital extension, not ours).

## Plan A — PWA performance + accessibility (`printit-web-app-for_end_user`)
1. **Fonts via `next/font/google`** (Inter + Space Mono):
   - Removes the render-blocking `@import` in `styles/globals.css` (the ~790–860 ms blocker
     → improves LCP) and self-hosts the fonts.
   - Applies `size-adjust`/fallback metrics so the font swap no longer reflows text — the
     primary contributor to the 0.229 greeting-section layout shift (CLS).
   - Wire in `_app.tsx`: load both fonts with `variable: '--font-sans'` / `--font-mono`,
     apply the variable classNames to the app wrapper; delete the `@import` and the
     `--font-sans`/`--font-mono` literal definitions in `globals.css`.
   - **Fallback** if `output:'export'` + next-pwa rejects next/font: keep the font
     stylesheet but add a `@font-face` fallback with `size-adjust` for Inter — same CLS
     benefit, no migration.
2. **Reserve space for async regions** so data loading doesn't shift layout: give the home
   wallet card, live-status card, and job-list their loading skeletons a **fixed min-height
   equal to the loaded state**. Confirm exact heights during implementation.
3. **Finish contrast:** raise `--text-subtle` (#444) to a grey meeting 4.5:1 on the dark
   background, and confirm the flagged classes (`greeting-label`, `mini-pill`,
   `section-title`, `live-meta`, `footer-powered-by`, `tab`). Brand accent button unchanged.
4. **`.browserslistrc`** with modern targets (Chrome/Edge ≥90, Firefox ≥90, Safari/iOS ≥15)
   so Next drops the legacy polyfills (`Array.at/flat/flatMap`, `Object.fromEntries`,
   `trimStart/End`).

**Verify:** `npx tsc --noEmit` + `npm run build`; re-run Lighthouse after deploy to confirm
CLS < 0.1 and the contrast audit passes. (No runtime test harness in this repo.)

## Plan B — Remove the `/admin/me` 403 console error (backend + PWA)
- **Backend (`cloud-backend`):** add `GET /auth/me` returning `200 {is_admin, is_guest,
  email}` for any authenticated user (no 403). `/admin/me` stays as the admin guard used by
  the dashboard. pytest: returns the flags for a normal user and a guest.
- **PWA:** `lib/api.ts checkIsAdmin` calls `/auth/me` (200) and reads `is_admin` instead of
  relying on `/admin/me`'s 403 → the browser console 403 disappears.

**Verify:** pytest (backend), `tsc`+build (PWA).

## Risks
- next/font with static export + next-pwa — low-but-nonzero; mitigated by the `@font-face`
  fallback.
- CLS < 0.1 not guaranteed in one pass; requires a post-deploy Lighthouse re-measure.
- Reserved heights must match loaded state or they trade one shift for whitespace — verify.

## Sequencing
Plan A first (self-contained, biggest score impact), then Plan B (small, cross-repo).
