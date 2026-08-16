# PWA Razorpay Loader Dedup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Replace the duplicated Razorpay checkout-script loader in `payment.tsx` and
`wallet.tsx` with one shared `lib/razorpay.ts` (singleton promise) — no behaviour change;
each page keeps its own error UX.

**Branch:** `perf/pwa-razorpay-loader` off `main`. **Dir:** `printit-web-app-for_end_user/`.
**Verify:** `npx tsc --noEmit` + `npm run build`.

---

### Task 1: `lib/razorpay.ts`
- [ ] Create `lib/razorpay.ts`:

```typescript
// Shared loader for the Razorpay checkout script (singleton; survives re-mounts).
const SCRIPT_ID = 'razorpay-checkout-js';
const SRC = 'https://checkout.razorpay.com/v1/checkout.js';
let _promise: Promise<boolean> | null = null;

/** Resolves true when the Razorpay script is available, false on load error. */
export function loadRazorpayScript(): Promise<boolean> {
  if (typeof document === 'undefined') return Promise.resolve(false);
  if ((window as any).Razorpay) return Promise.resolve(true);
  if (document.getElementById(SCRIPT_ID)) return Promise.resolve(true);
  if (_promise) return _promise;
  _promise = new Promise<boolean>((resolve) => {
    const script = document.createElement('script');
    script.id = SCRIPT_ID;
    script.src = SRC;
    script.onload = () => resolve(true);
    script.onerror = () => { _promise = null; resolve(false); };
    document.body.appendChild(script);
  });
  return _promise;
}
```

- [ ] Commit: `git add lib/razorpay.ts && git commit -m "feat(pwa): shared Razorpay script loader"`

---

### Task 2: Adopt in `payment.tsx`
- [ ] Add import near the top: `import { loadRazorpayScript } from '../lib/razorpay';`
- [ ] Replace the loader useEffect (lines ~94-118) with:

```tsx
  useEffect(() => {
    void loadRazorpayScript().then((ok) => {
      if (ok) { setRazorpayReady(true); return; }
      setModalTitle('Payment unavailable');
      setModalBody('Failed to load payment widget. Please refresh the page and try again.');
      setModalPrimaryLabel('OK');
      modalPrimaryActionRef.current = () => setModalOpen(false);
      setModalShowConfetti(false);
      setModalOpen(true);
    });
  }, []);
```

---

### Task 3: Adopt in `wallet.tsx`
- [ ] Add import: `import { loadRazorpayScript } from '../lib/razorpay';`
- [ ] Replace the loader useEffect (lines ~97-114) with:

```tsx
  useEffect(() => {
    void loadRazorpayScript().then((ok) => { setRazorpayReady(ok); });
  }, []);
```

- [ ] `npx tsc --noEmit` (no errors); `npm run build` (succeeds).
- [ ] Commit: `git add pages/payment.tsx pages/wallet.tsx && git commit -m "refactor(pwa): use shared Razorpay loader in payment + wallet"`

---

## Self-review
- **No behaviour change:** same script id/src; `setRazorpayReady(true)` on load/already-present;
  payment keeps its modal-on-error, wallet keeps `setRazorpayReady(false)` on error.
- **Verify:** tsc + build (no test runner).
