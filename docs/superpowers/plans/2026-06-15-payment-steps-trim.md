# Payment-Steps Trim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Cut the payment journey to 2 screens — merge the two `printers.tsx` modals into one
"Confirm & Pay" sheet and launch Razorpay inline, dropping the `/payment` page hop.

**Architecture:** All in `pages/printers.tsx`. `handlePayWithRazorpay` creates the order
(unchanged endpoints), loads the shared `lib/razorpay.ts` script, opens the Razorpay popup,
and verifies via `/payments/verify` — then routes home. The "Choose payment" modal is removed
and its two buttons move into the existing confirm sheet. `pages/payment.tsx` is left in place
but no longer navigated to.

**Tech Stack:** Next.js 14 PWA, TypeScript, Razorpay checkout. Verify: `npx tsc --noEmit` +
`npm run build` (no test runner). **Manual browser smoke test required before deploy.**

**Branch:** `feat/trim-payment-steps` off `main`. **Dir:** `printit-web-app-for_end_user/`.

---

### Task 0: Branch
- [ ] `git checkout -b feat/trim-payment-steps`

---

### Task 1: Inline Razorpay in `handlePayWithRazorpay`

**Files:** Modify `pages/printers.tsx`.

- [ ] **Step 1: Add the import** near the other lib imports (e.g. after the `apiJson` import):

```tsx
import { loadRazorpayScript } from '../lib/razorpay';
```

- [ ] **Step 2: Replace the whole `handlePayWithRazorpay` function** (the current version that
  does `router.push('/payment', ...)`) with this inline version plus a verify helper:

```tsx
  const verifyAndFinish = async (
    paymentId: number,
    resp: { razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string },
  ) => {
    if (!token) return;
    try {
      await apiJson(
        '/payments/verify',
        {
          method: 'POST',
          body: JSON.stringify({
            payment_id: paymentId,
            razorpay_order_id: resp.razorpay_order_id,
            razorpay_payment_id: resp.razorpay_payment_id,
            razorpay_signature: resp.razorpay_signature,
          }),
        },
        token,
      );
      router.push('/').catch(() => { });
    } catch (e: any) {
      setError(e?.message ?? 'Payment verification failed. If money was deducted it will be auto-refunded.');
      setLoading(false);
    }
  };

  const handlePayWithRazorpay = async () => {
    if (!token || !pendingPrinter || !hasJobContext) return;

    setError(null);
    setLoading(true);
    try {
      const printer = pendingPrinter;
      let order: PaymentOrder | MultiPaymentOrder | null = null;
      let description = '';

      if (isMulti && jobIds && jobIds.length > 1) {
        order = await apiJson<MultiPaymentOrder>(
          '/payments/jobs/order',
          { method: 'POST', body: JSON.stringify({ job_ids: jobIds, printer_id: printer.printer_id }) },
          token,
        );
        description = `${jobIds.length} jobs | ${printer.name}`;
      } else if (jobId != null) {
        order = await apiJson<PaymentOrder>(
          `/payments/job/${jobId}/printer/${encodeURIComponent(printer.printer_id)}/order`,
          { method: 'POST' },
          token,
        );
        description = `Job #${jobId} | ${printer.name}`;
      }

      if (!order) { setLoading(false); return; }

      const ok = await loadRazorpayScript();
      const RazorpayConstructor = (window as any).Razorpay;
      if (!ok || !RazorpayConstructor) {
        setError('Payment widget failed to load. Please try again.');
        setLoading(false);
        return;
      }

      const paymentId = order.payment_id;
      const amountPaise = Math.round(order.amount * 100);
      const rzp = new RazorpayConstructor({
        key: order.razorpay_key_id,
        amount: `${amountPaise}`,
        currency: order.currency,
        name: 'PrintIT',
        description,
        order_id: order.razorpay_order_id,
        handler: (response: any) => {
          void verifyAndFinish(paymentId, {
            razorpay_order_id: response.razorpay_order_id,
            razorpay_payment_id: response.razorpay_payment_id,
            razorpay_signature: response.razorpay_signature,
          });
        },
        modal: {
          ondismiss: () => {
            setError('Payment cancelled. No money was captured.');
            setLoading(false);
          },
        },
        theme: { color: 'var(--accent)' },
      });
      rzp.open();
      setKioskConfirmOpen(false);
    } catch (err: any) {
      setError(err?.message ?? 'Failed to create payment order');
      setLoading(false);
    }
  };
```

- [ ] **Step 3: Typecheck** `npx tsc --noEmit` → expect 0 (it will still pass even with the
  old second modal present; that's removed in Task 2).

---

### Task 2: Merge the two modals into one sheet

**Files:** Modify `pages/printers.tsx`.

- [ ] **Step 1: Delete the `paymentChoiceOpen` state.** Remove this line:

```tsx
  const [paymentChoiceOpen, setPaymentChoiceOpen] = useState(false);
```

- [ ] **Step 2: Delete `handleOpenPaymentChoice` and `handleConfirmKiosk`.** Remove:

```tsx
  const handleOpenPaymentChoice = () => {
    setKioskConfirmOpen(false);
    setPaymentChoiceOpen(true);
  };
```

and

```tsx
  const handleConfirmKiosk = async () => {
    if (!token || !pendingPrinter || !hasJobContext) return;

    handleOpenPaymentChoice();
  };
```

- [ ] **Step 3: In `handlePayWithWallet`, close the confirm sheet** (not the deleted
  paymentChoice). Change its `finally` block:

```tsx
    } finally {
      setLoading(false);
      setPaymentChoiceOpen(false);
      setPendingPrinter(null);
    }
```

to:

```tsx
    } finally {
      setLoading(false);
      setKioskConfirmOpen(false);
      setPendingPrinter(null);
    }
```

- [ ] **Step 4: Replace the confirm sheet's footer** (the `[Cancel] [Proceed]` block) with the
  payment buttons + error. Replace:

```tsx
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  type="button"
                  className="chip"
                  style={{ flex: 1, justifyContent: 'center' }}
                  onClick={() => { setKioskConfirmOpen(false); setPendingPrinter(null); }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn-select-files"
                  style={{ flex: 1, opacity: loading ? 0.7 : 1 }}
                  onClick={() => void handleConfirmKiosk()}
                  disabled={loading}
                >
                  Proceed
                </button>
              </div>
```

with:

```tsx
              {error && (
                <div style={{ fontSize: 13, color: '#ef4444', fontWeight: 600, marginBottom: 10 }}>{error}</div>
              )}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <button
                  type="button"
                  className="btn-select-files"
                  style={{ width: '100%', opacity: loading ? 0.7 : 1 }}
                  onClick={() => void handlePayWithRazorpay()}
                  disabled={loading}
                >
                  Pay with Razorpay
                </button>
                <button
                  type="button"
                  className="chip"
                  style={{ width: '100%', justifyContent: 'center', opacity: loading ? 0.7 : 1, padding: '12px 16px', borderRadius: 'var(--radius-md)' }}
                  onClick={() => void handlePayWithWallet()}
                  disabled={loading}
                >
                  Pay with Wallet
                </button>
                <button
                  type="button"
                  className="chip"
                  style={{ width: '100%', justifyContent: 'center' }}
                  onClick={() => { setKioskConfirmOpen(false); setPendingPrinter(null); setError(null); }}
                >
                  Cancel
                </button>
              </div>
```

- [ ] **Step 5: Rename the sheet header** for clarity. Change `<div style={{ fontWeight: 800 }}>Confirm kiosk</div>` to `<div style={{ fontWeight: 800 }}>Confirm &amp; pay</div>`.

- [ ] **Step 6: Delete the entire `paymentChoiceOpen` modal block** (the JSX starting at
  `{paymentChoiceOpen && pendingPrinter && (` through its closing `)}`).

- [ ] **Step 7: Typecheck + build:**

```bash
npx tsc --noEmit
npm run build
```

Expected: tsc 0 errors; build compiles, `/printers` emitted.

- [ ] **Step 8: Commit** (then revert generated artifacts):

```bash
git add pages/printers.tsx
git commit -m "feat(pwa): one-sheet confirm+pay with inline Razorpay (drop /payment hop)"
git checkout -- out public/sw.js public/.well-known/assetlinks.json 2>/dev/null; git clean -fd out >/dev/null 2>&1; rm -f tsconfig.tsbuildinfo
```

---

## Manual browser smoke test (REQUIRED before deploy)
Run `npm run dev`, then verify: single-job Razorpay success → home shows queued; multi-job
Razorpay success; Razorpay dismiss → "cancelled" message, no charge; Pay with Wallet (sufficient
balance) → home; Wallet insufficient → redirected to `/wallet`; out-of-paper printer still blocked.

## Self-review
- **Spec coverage:** merges modals (Task 2), inline Razorpay + verify (Task 1), wallet/out-of-paper
  unchanged, `/payment` left unused (no nav to it remains), step label now correct. ✓
- **Type consistency:** `order: PaymentOrder | MultiPaymentOrder | null`; `paymentId`, `order.amount`,
  `order.razorpay_key_id`, `order.currency`, `order.razorpay_order_id`, `order.payment_id` all exist on
  both order types (per `lib/types.ts`). `verifyAndFinish(paymentId, resp)` signature matches its call.
- **No placeholders.**
- **Risk:** payment-critical; mitigated by reusing the exact verify payload from `payment.tsx`, idempotent
  backend verify, disabled buttons during `loading`, and the required manual smoke test.
