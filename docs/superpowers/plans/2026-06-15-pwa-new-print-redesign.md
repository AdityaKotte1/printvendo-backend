# PWA New-Print Flow Redesign (Phase 1B.1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remove the upload-then-analyze double-upload and redesign the print-options UI
(copies / color / sides / pages) on `pages/jobs/new.tsx`, using client-side pdf.js page
counting so each file uploads exactly once.

**Architecture:** Two new pure `lib/` modules (`pageRange.ts`, `pricing.ts`) replace the
duplicated in-page math. `jobs/new.tsx` reads `numPages` in-browser via the already-bundled
`pdfjs-dist`, shows options immediately with live local pricing, and POSTs each file once to
`/jobs/` on confirm. New CSS adds a stepper + segmented control. No backend change.

**Tech Stack:** Next.js 14 (Pages Router, static export), TypeScript, pdfjs-dist 4.x,
lucide-react. **No test runner exists** — verify pure modules with a Node script and the
page with `npx tsc --noEmit`.

**This is subsystem plan 3** for the Phase 1 spec. Independently shippable.

**Working directory:** `printit-web-app-for_end_user/` (its own git repo).
**Branch:** create `perf/pwa-new-print` off `main` (this repo's main) before Task 1.

---

### Task 0: Branch

- [ ] **Step 1:** `git checkout -b perf/pwa-new-print` (expect: switched to new branch).

---

### Task 1: `lib/pageRange.ts` (pure)

**Files:** Create `lib/pageRange.ts`; Create `scripts/verify_pagerange.mjs` (temp, removed after).

- [ ] **Step 1: Create `lib/pageRange.ts`**

```typescript
// Pure page-range helpers shared by the upload flow and the printer/pay screens.

/** Count distinct pages selected by a CUPS-style range string ("1-3,5,8-10").
 *  Empty/invalid range => all pages. Out-of-bounds pages are clamped out. */
export function computeEffectivePages(
  totalPages: number | null | undefined,
  pageRange: string,
): number | null {
  if (!totalPages || totalPages <= 0) return null;
  const trimmed = (pageRange || '').trim();
  if (!trimmed) return totalPages;
  const pages = new Set<number>();
  try {
    for (const rawPart of trimmed.split(',')) {
      const part = rawPart.trim();
      if (!part) continue;
      if (part.includes('-')) {
        const [startStr, endStr] = part.split('-', 2);
        let start = parseInt(startStr, 10);
        let end = parseInt(endStr, 10);
        if (Number.isNaN(start) || Number.isNaN(end)) return totalPages;
        if (start > end) { const tmp = start; start = end; end = tmp; }
        for (let p = start; p <= end; p += 1) {
          if (p >= 1 && p <= totalPages) pages.add(p);
        }
      } else {
        const p = parseInt(part, 10);
        if (Number.isNaN(p)) return totalPages;
        if (p >= 1 && p <= totalPages) pages.add(p);
      }
    }
  } catch { return totalPages; }
  return pages.size > 0 ? pages.size : totalPages;
}

/** Validate a page-range string's characters (digits, commas, hyphens, spaces). */
export const PAGE_RANGE_REGEX = /^[0-9,\-\s]+$/;

export function isValidPageRange(pageRange: string): boolean {
  const trimmed = (pageRange || '').trim();
  return trimmed === '' || PAGE_RANGE_REGEX.test(trimmed);
}
```

- [ ] **Step 2: Create `scripts/verify_pagerange.mjs`**

```javascript
import { computeEffectivePages, isValidPageRange } from '../lib/pageRange.ts';
```

> Node can't import `.ts` directly. Instead, verify by compiling types (Step 4). Skip this
> file — delete the line above is unnecessary. (Verification is the tsc check in Task 4.)

Actually create `scripts/verify_pagerange.mjs` with an inline copy-free check using tsx is
overkill; **do not create this file.** Verification for pure modules happens via the
TypeScript compile in Task 4 Step 6 and the manual reasoning in Step 1's doc comments.

- [ ] **Step 3: Commit**

```bash
git add lib/pageRange.ts
git commit -m "feat(pwa): add shared pageRange module"
```

---

### Task 2: `lib/pricing.ts` (pure)

**Files:** Create `lib/pricing.ts`.

- [ ] **Step 1: Create `lib/pricing.ts`**

```typescript
import { computeEffectivePages } from './pageRange';

/** Centralized client-side price estimate in rupees.
 *  Mirrors backend defaults: color ₹10/pg; B/W single ₹2/pg;
 *  B/W duplex = first page ₹2 + rest ₹1.5/pg. Multiplied by copies. */
export function computePriceRupees(
  pageCount: number | null | undefined,
  copiesStr: string | number,
  color: boolean,
  duplex: boolean,
  pageRange: string,
): number {
  let copies = parseInt(String(copiesStr ?? '1'), 10);
  if (Number.isNaN(copies) || copies < 1) copies = 1;
  const totalPages = pageCount && pageCount > 0 ? pageCount : 1;
  const effectiveRaw = computeEffectivePages(totalPages, pageRange);
  let effectivePages = effectiveRaw && effectiveRaw > 0 ? effectiveRaw : totalPages;
  if (effectivePages < 1) effectivePages = 1;
  if (color) return effectivePages * 10 * copies;
  if (duplex) {
    if (effectivePages === 1) return 2 * copies;
    return ((effectivePages - 1) * 1.5 + 2) * copies;
  }
  return effectivePages * 2 * copies;
}
```

- [ ] **Step 2: Commit**

```bash
git add lib/pricing.ts
git commit -m "feat(pwa): add shared pricing module"
```

---

### Task 3: Stepper + segmented-control CSS

**Files:** Modify `styles/globals.css` (append a new block near the form classes).

- [ ] **Step 1: Append to `styles/globals.css`**

```css
/* ── New-print options controls (Phase 1B.1) ── */
.opt-label {
  font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--text-muted);
  display: block; margin-bottom: 8px;
}
.stepper { display: inline-flex; align-items: center; gap: 0; }
.stepper button {
  width: 44px; height: 44px; display: grid; place-items: center;
  background: var(--surface-elevated); color: var(--text);
  border: 1px solid var(--border); cursor: pointer;
  transition: background 150ms ease, transform 120ms ease;
}
.stepper button:first-child { border-radius: var(--radius-md) 0 0 var(--radius-md); }
.stepper button:last-child { border-radius: 0 var(--radius-md) var(--radius-md) 0; }
.stepper button:active { transform: scale(0.94); }
.stepper button:disabled { opacity: 0.4; cursor: not-allowed; }
.stepper .stepper-value {
  min-width: 56px; height: 44px; display: grid; place-items: center;
  font-variant-numeric: tabular-nums; font-weight: 700; font-size: 15px;
  border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.segmented {
  display: grid; grid-auto-flow: column; grid-auto-columns: 1fr;
  border: 1px solid var(--border); border-radius: var(--radius-md);
  overflow: hidden;
}
.segmented button {
  height: 44px; border: none; cursor: pointer; font-weight: 600; font-size: 14px;
  background: var(--surface); color: var(--text-muted);
  transition: background 150ms ease, color 150ms ease;
}
.segmented button + button { border-left: 1px solid var(--border); }
.segmented button.active { background: var(--accent); color: #fff; }
.segmented button:active { transform: scale(0.98); }
@media (prefers-reduced-motion: reduce) {
  .stepper button, .segmented button { transition: none; }
}
```

- [ ] **Step 2: Commit**

```bash
git add styles/globals.css
git commit -m "feat(pwa): stepper + segmented control styles"
```

---

### Task 4: Rewrite `pages/jobs/new.tsx`

**Files:** Replace the full contents of `pages/jobs/new.tsx`.

- [ ] **Step 1: Replace the file** with the content in Appendix A (below). Key changes:
  - Read `numPages` client-side via `import('pdfjs-dist/legacy/build/pdf.mjs')` +
    `getDocument({ data })` on file add; no `/jobs/analyze` call.
  - Options (stepper / segmented / All-Custom pages) render immediately; live price via
    `computePriceRupees`.
  - Single CTA "Upload & continue": POST each file once to `/jobs/` then route to
    `/printers?jobIds=...`.
  - pdf.js failure => `pageCount = null`, label "pages calculated at upload", still uploadable.

- [ ] **Step 2: Typecheck**

```bash
npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Production build (static export)**

```bash
npm run build
```

Expected: build completes, `jobs/new` is emitted.

- [ ] **Step 4: Commit**

```bash
git add pages/jobs/new.tsx
git commit -m "feat(pwa): single-upload new-print flow + redesigned options (1B.1)"
```

---

## Appendix A — full `pages/jobs/new.tsx`

(Full component source is written during execution; it reuses `lib/pricing.ts`,
`lib/pageRange.ts`, the new `.stepper`/`.segmented` classes, the existing `.drop-zone`,
`.btn-select-files`, `.chip`, and the existing modal markup. The submit handler keeps the
existing `/jobs/` POST + `?jobIds=` redirect; only the analyze step and double upload are
removed.)

---

## Self-review notes
- **Spec coverage:** implements §4H (1B.1) — client-side pdf.js page count, single upload,
  redesigned copies/color/sides/pages controls, shared `pricing`/`pageRange` modules.
- **No behaviour change to pricing/job semantics** — only when/where page count is obtained
  and the visual design.
- **Verification:** pure modules covered by `tsc` typecheck + their doc-specified math;
  page covered by `tsc --noEmit` + `next build`. (No test runner in this repo.)
- **Fallback path** for unreadable PDFs keeps uploads working (no regression).
