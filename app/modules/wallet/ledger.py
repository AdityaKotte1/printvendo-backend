"""Money going into and out of a student's balance.

Two rules carry this module, and both are enforced by the database rather than
by Python:

* **A balance cannot be spent twice.** Reading a balance, comparing it in
  Python and writing it back lets two concurrent requests both pass the check.
  Every debit here is a single conditional UPDATE -- no row returned means
  insufficient funds, decided by Postgres under a row lock.
* **A retried webhook credits once.** `UNIQUE (wallet_id, reference)` does that,
  rather than a "have we seen this already" query that two deliveries can both
  answer no to.

The ledger is the record and the balance column is what makes the conditional
UPDATE possible. They are kept identical by construction -- every movement
writes both, in one transaction -- and a property test asserts
`sum(entries) == balance` over a generated history rather than one example.
"""

from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.core.money import as_money
from app.modules.wallet.models import EntryKind, Wallet, WalletEntry

NOT_ENOUGH = "There is not enough balance in your wallet for that."
ALREADY_RECORDED = "That wallet movement has already been recorded."
MUST_BE_POSITIVE = "A wallet amount must be more than zero."


def wallet_for(db: Session, *, user_id: int) -> Wallet:
    """This student's wallet, creating it if this is the first time.

    Lazy on purpose: a student who has never topped up has a zero balance
    whether or not a row exists, so creating one at registration would buy a
    backfill and a "user exists but wallet does not" state, and nothing else.
    """
    wallet = db.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    ).scalar_one_or_none()
    if wallet is not None:
        return wallet

    wallet = Wallet(user_id=user_id, balance_inr=Decimal("0.00"))
    db.add(wallet)
    db.flush()
    return wallet


def balance_of(db: Session, *, user_id: int) -> Decimal:
    """What this student can spend. Zero when there is no wallet yet."""
    balance = db.execute(
        select(Wallet.balance_inr).where(Wallet.user_id == user_id)
    ).scalar_one_or_none()
    return as_money(balance) if balance is not None else Decimal("0.00")


def _move(
    db: Session,
    *,
    wallet: Wallet,
    kind: EntryKind,
    amount: Decimal,
    reference: str,
    note: str | None,
    require_funds: bool,
) -> WalletEntry:
    """Move the balance and write the ledger row, or do neither.

    Both inside **one savepoint**, for a reason found by testing rather than by
    design: a duplicate reference raises IntegrityError, which aborts the whole
    transaction. Without the savepoint the balance change would already have
    happened and could not be undone without also discarding everything else the
    request had done -- and the caller would be handed a session it could no
    longer use. Rolling back to the savepoint undoes exactly this movement and
    leaves the surrounding transaction intact.

    The unique constraint stays the guard. Checking for the reference first with
    a SELECT would be the read-check-write that this module exists to avoid,
    and two concurrent replays would both find nothing.
    """
    condition = [Wallet.id == wallet.id]
    if require_funds:
        # The check lives in the WHERE clause. A second request arriving while
        # the first is open blocks on the row, then evaluates against the
        # balance the first one actually left.
        condition.append(Wallet.balance_inr >= -amount)

    try:
        with db.begin_nested():
            new_balance = db.execute(
                update(Wallet)
                .where(*condition)
                .values(balance_inr=Wallet.balance_inr + amount)
                .returning(Wallet.balance_inr)
            ).scalar_one_or_none()

            if new_balance is None:
                raise BadRequest(NOT_ENOUGH)

            entry = WalletEntry(
                wallet_id=wallet.id,
                kind=kind,
                amount_inr=amount,
                balance_after_inr=as_money(new_balance),
                reference=reference,
                note=note,
            )
            db.add(entry)
            db.flush()
    except IntegrityError as exc:
        raise Conflict(ALREADY_RECORDED) from exc

    db.refresh(wallet)
    return entry


def credit(
    db: Session,
    *,
    user_id: int,
    amount: Decimal,
    kind: EntryKind,
    reference: str,
    note: str | None = None,
) -> WalletEntry:
    """Put money in: a top-up, a refund, a promotion, an adjustment."""
    amount = as_money(amount)
    if amount <= Decimal("0.00"):
        raise BadRequest(MUST_BE_POSITIVE)

    wallet = wallet_for(db, user_id=user_id)

    # An UPDATE rather than an attribute assignment: two concurrent credits must
    # add to each other rather than one overwriting a value the other read.
    return _move(
        db,
        wallet=wallet,
        kind=kind,
        amount=amount,
        reference=reference,
        note=note,
        require_funds=False,
    )


def debit(
    db: Session,
    *,
    user_id: int,
    amount: Decimal,
    reference: str,
    note: str | None = None,
) -> WalletEntry:
    """Take money out, or refuse. Never both, and never twice.

    The condition lives in the WHERE clause. A second request arriving while the
    first is still open blocks on the row, and when it proceeds it evaluates
    against the balance the first one left -- so the check it passed is the
    balance that actually exists, not one it read a moment ago.
    """
    amount = as_money(amount)
    if amount <= Decimal("0.00"):
        raise BadRequest(MUST_BE_POSITIVE)

    wallet = db.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    ).scalar_one_or_none()
    if wallet is None:
        # No wallet means no money. Same sentence as an insufficient balance:
        # they are the same fact from the student's side.
        raise BadRequest(NOT_ENOUGH)

    return _move(
        db,
        wallet=wallet,
        kind=EntryKind.SPEND,
        amount=-amount,
        reference=reference,
        note=note,
        require_funds=True,
    )


def statement(
    db: Session, *, user_id: int, limit: int = 100
) -> list[WalletEntry]:
    """What happened, newest first."""
    wallet = db.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    ).scalar_one_or_none()
    if wallet is None:
        return []

    return list(
        db.execute(
            select(WalletEntry)
            .where(WalletEntry.wallet_id == wallet.id)
            .order_by(WalletEntry.created_at.desc(), WalletEntry.id.desc())
            .limit(limit)
        ).scalars()
    )
