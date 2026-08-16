# Payment-Steps Trim (PWA) — Design Spec

**Date:** 2026-06-15 · **Status:** Approved design, ready for plan · **Approach:** B (merge modals + inline Razorpay)

## Goal
Cut the end-user payment journey from 4 screens to 2 by (1) merging the two sequential
modals on `printers.tsx` into one "Confirm & Pay" sheet, and (2) launching the Razorpay
checkout **inline** from that sheet — removing the separate `/payment` page hop.

## Current vs target
- **Today (Razorpay):** tap printer → "Confirm kiosk" modal → "Choose payment" modal →
  `/payment` page → "Continue" button → Razorpay popup.
- **Today (Wallet):** tap printer → "Confirm kiosk" → "Choose payment" → done.
- **Target (Razorpay):** tap printer → **"Confirm & Pay" sheet** → Razorpay popup → home.
- **Target (Wallet):** tap printer → **"Confirm & Pay" sheet** → home.

## Scope (all in `printit-web-app-for_end_user`)
1. **Merge the two modals** in `pages/printers.tsx`:
   - Keep the existing "Confirm kiosk" sheet content (the physically-at-kiosk note, printer
     name/id, optional custom-pricing offer block, and the Price Summary).
   - Replace its `[Cancel] [Proceed]` footer with `[Pay with Razorpay] [Pay with Wallet]`
     plus a Cancel/Close.
   - Delete the second `paymentChoiceOpen` modal, the `paymentChoiceOpen` state,
     `handleOpenPaymentChoice`, and `handleConfirmKiosk` (no longer needed).
   - `handlePayWithWallet` / `handlePayWithRazorpay` now close the single sheet.
2. **Inline Razorpay** in `handlePayWithRazorpay`:
   - After creating the order (existing `/payments/jobs/order` or
     `/payments/job/{id}/printer/{pid}/order` call), call `loadRazorpayScript()` (the
     shared `lib/razorpay.ts` loader), then open `new window.Razorpay({...})` with the
     order's key/amount/currency/order_id and a `description` (`"{n} jobs | {printer}"` or
     `"Job #{id} | {printer}"`).
   - **handler** → POST `/payments/verify` with `{payment_id, razorpay_order_id,
     razorpay_payment_id, razorpay_signature}`; on success `router.push('/')` (home shows
     the queued job); on failure `setError(...)`.
   - **modal.ondismiss** → `setError('Payment cancelled. No money was captured.')` and clear
     loading. Keep `theme.color` as today.
   - On script-load failure → `setError(...)` and abort (no charge created beyond the
     unpaid order, same as today if the user backs out).
3. **`/payment` page (`pages/payment.tsx`):** **left in place but no longer navigated to**
   (avoids 404 for any in-flight/bookmarked link). Marked unused; safe to delete in a later
   cleanup. No other route links to it (wallet top-up uses its own inline Razorpay).
4. **Step label:** `printers.tsx` "Step 2 of 2" is now accurate (it is the last step) — no
   change needed; the `/payment` "Final step" label is moot since the page is unused.

## Behaviour preserved
- Same order-creation endpoints, same `/payments/verify` payload, same wallet flow
  (`/wallet/hold(/multi)` + `/printers/{id}/print`), same out-of-paper guard, same
  insufficient-wallet → `/wallet` redirect.
- Only the *number of screens* changes and *where* the Razorpay popup is launched from.

## Risks & mitigations
- **Payment-critical, no runtime test harness** (only `tsc` + `next build`). Mitigation:
  reuse the proven verify payload/handler shape from `payment.tsx` verbatim; keep the order
  + verify endpoints unchanged; **manual browser smoke test required before deploy** (test:
  single + multi job, Razorpay success, Razorpay dismiss/cancel, wallet, insufficient
  wallet).
- **Double-charge / re-entry:** disable the pay buttons while `loading` (as today); the
  backend `/payments/verify` is idempotent and `/payments/job/.../refresh` exists as a
  safety net.
- **Razorpay theme `var(--accent)`** may not resolve in the SDK config — keep exactly as
  the current `/payment` page passes it (no behaviour change).

## Testing
`npx tsc --noEmit` + `npm run build`; then the manual browser smoke test above. (No unit
runner in this repo.)

## Out of scope
Deleting `payment.tsx`; any backend change; the other Phase-2 UX items.
