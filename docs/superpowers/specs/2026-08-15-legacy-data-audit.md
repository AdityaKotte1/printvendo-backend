# Legacy Production Data — Audit

**Date:** 2026-08-15
**Source:** `db-20260815-020001.dump`, restored locally as `printit_legacy`
**Purpose:** find what has to be cleaned, decided, or repaired before the data moves to the new backend

> **Counts are indicative, not current.** This is a nightly dump taken at a point
> in time, not a live read. The *patterns* are what matter here; exact numbers
> will be recomputed at migration time by the reconciliation report.

---

## Summary

The database is in better structural shape than the code around it. **Referential
integrity is intact** — zero orphaned jobs, payments, printer_jobs or ledger
rows. Nothing is corrupt.

What is wrong is **semantic**: fields that mean two things, states nothing ever
leaves, and statuses the model does not document. Those are the things that make
the system confusing to work on, and every one of them is a decision that has to
be made explicitly rather than carried across.

| Severity | Finding |
|---|---|
| **High** | Payment status is an undocumented six-value enum, queried inconsistently across the codebase |
| **High** | A `SOLD` kiosk with no owner — its takings route to the platform |
| **High** | Wallet balances that disagree with their own ledger |
| Medium | Onboarding was never used: every kiosk sits in the initial state |
| Medium | Paid orders that produced no print job |
| Medium | Refunds parked in a status nothing processes |
| Medium | Case-duplicate email addresses that will collide with the new unique constraint |
| Low | Test and development kiosks in live data; probable duplicate kiosk |
| Low | Large volume of abandoned checkouts and empty guest accounts |

---

## 1. Payment status means more than the model says

`app/models/payment.py` documents three values:

```python
status = Column(String, default="CREATED")  # CREATED, PAID, FAILED
```

Production contains **six**: `PAID`, `CREATED`, `CAPTURED`, `REFUND_PENDING`,
`REFUNDED`, `FAILED`.

`CAPTURED` is not a Razorpay concept here — it is what a **wallet** payment
becomes once the print succeeds (`routers/pi.py:615`). Every `CAPTURED` row in
the dump carries `razorpay_order_id LIKE 'WALLET:%'` and no `razorpay_payment_id`,
which confirms it: PAID means the money is held, CAPTURED means the print
happened and the money is earned.

**The problem is that the codebase does not agree with itself about this.**

- `routers/kiosk.py` — owner earnings and analytics — correctly counts
  `status IN ('PAID','CAPTURED')`, in eight separate places.
- `routers/wallet.py`, `routers/printers.py` and `routers/pi.py` filter on
  `status == 'PAID'` alone.

So the same concept — "this payment succeeded" — is expressed two different ways
in one codebase, and roughly a sixth of successful payments are invisible to the
second form.

**Concrete consequence.** The wallet idempotency guard in `POST /wallet/hold`
looks for an existing payment with `status == "PAID"`. Once a payment has moved
to `CAPTURED`, that guard no longer sees it, so the request is treated as new.

**Decision for the new backend:** the status set is an enum with every value
named and documented, and "did this succeed" is a single predicate that no
caller re-expresses as a status comparison. Migration maps both `PAID` and
`CAPTURED` onto the new `Order`/`Payment` states explicitly rather than copying
the string across.

---

## 2. A `SOLD` kiosk with no owner

`VIGNAN-1 XEROX` (id 35) is `kiosk_type = SOLD` with **zero rows in
`printer_owners`**.

`SOLD` means the shop bought the hardware and collects into their own Razorpay.
But `gateway_routing.owner_of()` resolves the owner through `printer_owners`,
finds nothing, returns `None`, and the platform's keys take the payment.

So this kiosk is labelled as belonging to a shop while its takings arrive in the
platform account — and because settlements are retired, there is no mechanism
that would ever notice or correct it.

Two other `SOLD` kiosks do have an owner, so this is a data gap rather than a
design failure.

**This needs your decision before migration**, and it is the item spec §8 flags
as "not auto-decided: getting it wrong routes money to the wrong Razorpay
account." Either the kiosk has an owner who should be linked, or it is not
really `SOLD`.

---

## 3. Wallet balances that disagree with their ledger

A small number of wallets have `balance <> SUM(ledger WHERE status='SUCCESS')`.

The ledger is meant to be the record and the balance a cached total, so any
disagreement means one of them is wrong. Given `wallet_ledger` carries
idempotency keys and unique constraints on `razorpay_payment_id`, the ledger is
the more trustworthy of the two.

**Migration rule:** rebuild every balance from its ledger rather than copying the
`balance` column, and report every wallet where the two differed, with the
delta, so the discrepancies are visible rather than silently corrected. Total
wallet liability is small — low thousands of rupees — so the exposure is
bounded either way.

---

## 4. Onboarding was never used

**Every kiosk in production is `onboarding_stage = REGISTERED`.** All of them,
including ones that have been taking payments for months.

The onboarding ladder and its LIVE gate were built (`routers/owner.py:1676`) but
nothing was ever moved through them. The field is decoration: the real
"is this kiosk usable" signals are `is_approved` and `is_active`.

That matters in two ways:

1. There is no legacy onboarding state worth preserving. Everything arrives as
   `REGISTERED`, and each kiosk needs a deliberate promotion in the new system.
2. It explains why the `SOLD`-kiosk-with-no-owner case above went unnoticed —
   the gate that would have caught it was never in the path.

**Migration rule:** derive the new stage from what is actually true —
`is_approved` and `is_active` and whether the owner can collect — rather than
copying `onboarding_stage`. Kiosks that cannot pass the gate arrive as
`APPROVED`, not `LIVE`.

---

## 5. Paid, but nothing printed

A handful of payments are `PAID` with no corresponding `printer_jobs` row.

This is the two-call hazard the spec predicts: `POST /wallet/hold` marks the
payment paid but does not create the print job, and the client must remember a
second `POST /printers/{id}/print`. When that second call does not land, the
student has paid for a job that never entered a queue.

The rupee value is trivial. The pattern is not — it is the single clearest
argument for the `Order` aggregate, where payment and print tasks commit in one
transaction and this state cannot exist.

**Migration rule:** these become `Order`s in a terminal failed state with a
refund owed, listed in the reconciliation report by student, not silently
imported as completed.

---

## 6. Refunds parked in a status nothing processes

`REFUND_PENDING` accounts for a few hundred payments and several thousand
rupees.

It is written in exactly one place (`routers/pi.py:355`, when a Pi reports a
failed print) and the comment says the payment is marked "for operator review".
Nothing in the codebase reviews them, and no dashboard lists them. They simply
accumulate.

**Migration rule:** these are real money owed to students. They import as
refund-owed and appear in the reconciliation report as a worklist, not as
resolved payments.

---

## 7. Case-duplicate email addresses

Around ten addresses differ only by capitalisation — `Person@example.com` and
`person@example.com` as separate accounts.

Postgres string comparison is case-sensitive and the old backend queried
`User.email == email` directly, so both rows were reachable and each accumulated
its own history and wallet.

The new schema matches email case-insensitively and holds a unique constraint,
so **these will collide on import.**

**Decision needed:** merge each pair (keeping the older account and moving jobs,
payments and wallet balance onto it) or keep one and deactivate the other. Merge
is the honest option where either side has a wallet balance — the student paid
that money.

---

## 8. Development and test data in production

Live kiosks include `Dev-Printer-1` and `TEST 1`. There is also a probable
duplicate: `calcut` (id 16) and `calicut` (id 17) — one looks like a typo of the
other, both are `PLATFORM`, both approved.

Two kiosks (`Jayanagar-PrintIT`, `iEX Copier`) are `is_approved = false` but
still present and wallet-enabled.

**Migration rule:** quarantine rather than delete, with a list for you to
confirm. A kiosk with real payments against it cannot simply be dropped, even if
it was a test.

---

## 9. Volume that is simply noise

- **~4,000 `CREATED` payments** (~₹35k notional) — abandoned checkouts. Never
  cleaned up, and they inflate every count that does not filter on status.
- **~800 guest accounts, roughly half with no payment at all** — a guest is
  created on every "continue without an account" tap, and most never buy
  anything.
- **~12,000 refresh tokens** — never pruned, including long-expired ones.

None of this is harmful. It is the difference between a database that has been
maintained and one that has only been written to.

**Migration rule:** abandoned checkouts and payment-free guests are quarantined,
not imported. Refresh tokens are not migrated at all — everyone signs in again,
which is a free security reset.

---

## What this means for the cutover

The reassuring finding is that **nothing is broken at the storage level**. There
are no orphans, no corruption, no dangling foreign keys. The data can be moved.

What cannot be moved without decisions is the meaning: a status field with two
values for one concept, a kiosk labelled as someone's that isn't, balances that
disagree with their own ledger, and a state machine nothing ever entered.

Every one of those is a question the migration has to ask out loud rather than
answer by copying a column. The reconciliation report in spec §8 is where they
get asked: row counts in, out and quarantined per table, plus wallet-balance and
revenue totals that must match production exactly or the run fails.

**Three items need your decision before the migration can be written:**

1. `VIGNAN-1 XEROX` — link an owner, or change its type
2. The case-duplicate accounts — merge or deactivate
3. The test and duplicate kiosks — confirm which to quarantine

The rest is mechanical.
