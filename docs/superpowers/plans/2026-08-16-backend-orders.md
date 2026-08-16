# PrintVendo Backend — Orders Implementation Plan

> **For agentic workers:** TDD, and mutation-test every money rule. See CLAUDE.md, "How this work is done".

**Goal:** One student, one kiosk, N documents, one payment, N print tasks — **created in a single transaction**, so "paid but never printed" is unreachable rather than unlikely.

**Depends on:** foundation, identity, kiosks, payment gate, billing, printing (complete — 756 tests)

---

## What is actually wrong today

### O1 — Nothing owns the transaction

`Job`, `PrinterJob` and `Payment` each point independently at a user and a
printer. `POST /wallet/hold` marks a `Payment` **PAID** and creates **no**
`PrinterJob`. The caller must remember a second `POST /printers/{id}/print`.
Miss it — network drop, closed tab, crash between the two — and the student has
paid for a job that will never print.

`printvendo-web/CLAUDE.md` documents this as a live hazard. It is not a race
that is hard to hit; it is the ordinary failure of any two-call sequence.

### O2 — One payment cannot span three files

`Payment.job_id` is `NOT NULL`, so a payment belongs to exactly one document.
Printing three files therefore needed a whole second endpoint,
`/wallet/hold/multi`, which writes one ledger row and N payment rows sharing a
synthetic `razorpay_order_id` of `"WALLET:{ledger_id}"`.

Two code paths for one user action, and the second exists only to work around a
column constraint.

### O3 — The price is computed in more than one place

The server prices a job; the client also estimates it (`lib/price.ts`). The
estimate is display-only and that is correct — but the *fee* is added in
`payments.py` rather than with the price, so what a student is quoted and what
they are charged are assembled by different code.

---

## The design

### D-O1 — `Order` is the aggregate, and paying is one commit

```
Order   DRAFT → AWAITING_PAYMENT → PAID → DISPATCHED → COMPLETED
                     ↓               ↓                     ↓
                  EXPIRED        REFUNDED          PARTIALLY_FAILED
```

`mark_paid()` is the **only** way an order becomes PAID, and in the same
transaction it creates every `PrintTask`. There is no ordering of two calls to
get wrong, because there is one call.

Wallet and gateway are two branches *into* that one commit, not two flows.

### D-O2 — Price and fee are quoted once, and stored

An order stores what it quoted: `subtotal_inr`, `fee_inr`, `total_inr`. Not
recomputed at payment time — a price band edited between quote and payment must
not silently change what a student is charged after they have seen a number.

Per document: `workload()` from the printing module gives `impressions`, and the
kiosk's own prices give the rate. One calculation, already the single source for
paper and for the device.

### D-O3 — The fee follows D9, and wallet has none

`min(2% of subtotal, ₹2)` on **every gateway payment**, including at
owner-gateway kiosks where the owner keeps it. That is a recorded commercial
decision (D9), not an oversight — do not "fix" it.

Wallet spending carries **no** fee (D8), and wallet is only spendable where the
gate says the platform collects.

### D-O4 — Paper is checked, not held

A kiosk refuses an order it visibly cannot print: the tray, minus the
`predicted_sheets` of tasks that have not finished, must cover it. Derived, not
counted -- no `reserved` column to drift out of step with the queue the way the
old tray counter drifted from the physical tray.

**An unpaid order holds nothing.** Decided by the operator on 2026-08-16, after
an earlier draft did hold it. A basket somebody opened and wandered away from
should not stop the next student printing.

The consequence is accepted deliberately: two students can both be accepted for
the same last sheets, and the second job waits `BLOCKED` at the device until
somebody refills. That is visible and recoverable. The alternative -- a busy
kiosk refusing work it could have done -- is neither.

### D-O5 — The gate decides whether an order may exist at all

`kiosk_payment_gate(kiosk)` returning `CLOSED` refuses the order. Same function
the kiosk listing and the refund destination use. Nothing re-derives it.

### D-O6 — Money is `Decimal` rupees, and `float` is refused

`app.core.money.as_money` raises on a `float` rather than accepting a value that
has already lost precision.

---

## Task list

1. **Order + OrderItem models + migration** — states, opaque `ord_` ids, `legacy_id`.
2. **Quoting** — per-document price from the kiosk's rates and `workload()`; the D9 fee; one function.
3. **Placing an order** — gate check, paper availability, quote, `AWAITING_PAYMENT`.
4. **`mark_paid`** — the single transaction: order PAID, payment recorded, N `PrintTask`s created, in the student's chosen order.
5. **Cancellation and expiry** — an unpaid order releases nothing but its place; an expired one cannot later be paid.
6. **Repository + module surface + authz matrix entries.**

Tasks 3 and 4 are where correctness lives.

---

## Done when

- There is no code path that moves money without creating print tasks, and a test proves the failure of task creation rolls the payment back
- An order of three documents produces three tasks, in the student's order, from one payment
- A kiosk without enough paper refuses the order rather than accepting and failing at the printer
- Paper already committed to queued tasks counts against availability, while an unpaid order holds none
- A `CLOSED` kiosk cannot take an order
- The fee is `min(2%, ₹2)` on gateway, zero on wallet, and mutating either fails a test
- A price change between quote and payment does not change what the student is charged
- Paying an expired or already-paid order is refused, and does not create a second set of tasks
