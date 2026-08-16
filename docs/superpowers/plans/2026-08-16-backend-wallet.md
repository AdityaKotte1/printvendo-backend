# PrintVendo Backend — Wallet Implementation Plan

> **For agentic workers:** TDD, and mutation-test every money rule. See CLAUDE.md, "How this work is done".

**Goal:** A student's balance that cannot be spent twice, cannot drift from its own ledger, and cannot be credited twice by a retried webhook.

**Depends on:** foundation, identity, kiosks, payment gate, orders (complete — 811 tests)

---

## What is actually wrong today

### W1 — The ledger has a `status`, so it does not record facts

`WalletLedger.status` is `PENDING | SUCCESS | FAILED`. A row therefore might not
have happened. Any sum over the ledger has to know which statuses count, and
every consumer that gets that predicate wrong disagrees with `Wallet.balance` —
which is exactly the class of defect this rewrite exists to remove
(`wallet.py`/`printers.py` filter `PAID` only where `kiosk.py` filters
`PAID`+`CAPTURED`).

### W2 — `HOLD` exists only to paper over the two-call hazard

`POST /wallet/hold` debits and marks a payment PAID, then the caller is supposed
to make a second request to actually enqueue the print. `HOLD` and `CAPTURE`
entry types exist to model the gap between the two.

The `Order` aggregate closed that gap: money moves and print tasks are created
in one commit. **There is no gap left to model**, so there are no holds.

### W3 — Read-check-write is a double-spend

Reading a balance, comparing it in Python and then writing it back lets two
concurrent requests both pass the check. The old backend fixed this with a
single conditional `UPDATE`; that fix is carried over deliberately and pinned by
a concurrency test using two real transactions.

---

## The design

### D-W1 — Every ledger entry is something that already happened

No `status`. A row exists because money moved. The sum of a wallet's entries
**is** its balance, and a property test asserts that over generated histories
rather than over one happy example.

### D-W2 — Balance is a column, moved only by a conditional UPDATE

```sql
UPDATE wallets SET balance_inr = balance_inr - :amount
WHERE id = :id AND balance_inr >= :amount
RETURNING balance_inr
```

No row returned means insufficient funds — decided by Postgres, not by a Python
comparison that two requests can both pass.

The column is not a second source of truth: `sum(entries) == balance` is an
invariant, and the ledger is what a dispute is settled from. The column exists
because a conditional UPDATE needs something to be conditional on.

### D-W3 — A reference is unique per wallet, so a replay is a no-op

Every entry carries the reference for the thing that caused it — a Razorpay
payment id, an order id. `UNIQUE (wallet_id, reference)` is what makes a
webhook delivered three times credit once. Same mechanism as the old
nullable-unique `razorpay_payment_id`, applied to every entry kind rather than
to top-ups alone.

### D-W4 — Spending is only possible where the platform collects

The gate decides (`wallet_may_be_spent`). Top-ups land in the platform's
Razorpay, so spending that balance at an owner-gateway kiosk would have the
platform keep the cash while the owner prints for free. Enforced in the order
path, and the wallet refuses to be the second place that decides it.

### D-W5 — A wallet is created on demand, never by a registration hook

A student who has never topped up has a zero balance whether or not a row
exists. Creating one lazily means no backfill, and no "user exists but wallet
does not" state for anything to trip over.

---

## Task list

1. **Wallet + WalletEntry models + migration.**
2. **Credit and debit** — conditional UPDATE, ledger entry, reference uniqueness.
3. **Balance and history** — derived-sum invariant, statement listing.
4. **Wiring into `mark_paid`** — the wallet branch of the one commit.
5. **Module surface + contract.**

---

## Done when

- Two concurrent debits of a balance that only covers one leave exactly one succeeding, proven with real transactions
- `sum(entries) == balance` holds over a generated history of credits, debits and refunds
- The same reference credited twice credits once
- A debit larger than the balance is refused and writes no ledger entry
- Spending at a kiosk where the platform does not collect is refused
- Paying an order by wallet debits and creates print tasks in one commit, and a failure in either leaves neither
