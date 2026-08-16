"""Money going into and out of a student's balance.

The two rules that matter are that it cannot be spent twice and cannot drift
from its own ledger. Both are tested against real Postgres transactions, because
a mock cannot fail the way a database can.
"""

import random
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.db import get_engine
from app.core.errors import BadRequest, Conflict
from app.modules.identity.models import User
from app.modules.wallet.ledger import (
    balance_of,
    credit,
    debit,
    statement,
    wallet_for,
)
from app.modules.wallet.models import EntryKind, Wallet, WalletEntry


@pytest.fixture
def user(db_session):
    user = User(email="wallet@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


# ── the wallet itself ───────────────────────────────────────────────────────


def test_a_student_who_has_never_topped_up_has_a_zero_balance(db_session, user):
    """No row yet, and no need for one. Creating wallets at registration would
    mean a backfill and a "user exists but wallet does not" state."""
    assert balance_of(db_session, user_id=user.id) == Decimal("0.00")
    assert db_session.query(Wallet).count() == 0


def test_a_wallet_is_created_the_first_time_it_is_needed(db_session, user):
    wallet = wallet_for(db_session, user_id=user.id)

    assert wallet.balance_inr == Decimal("0.00")
    assert db_session.query(Wallet).count() == 1


def test_asking_twice_does_not_make_two_wallets(db_session, user):
    first = wallet_for(db_session, user_id=user.id)
    second = wallet_for(db_session, user_id=user.id)

    assert first.id == second.id


# ── crediting ───────────────────────────────────────────────────────────────


def test_a_topup_raises_the_balance_and_records_why(db_session, user):
    entry = credit(
        db_session,
        user_id=user.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_ABC123",
    )

    assert balance_of(db_session, user_id=user.id) == Decimal("100.00")
    assert entry.amount_inr == Decimal("100.00")
    assert entry.balance_after_inr == Decimal("100.00")
    assert entry.reference == "pay_ABC123"


def test_the_same_reference_credits_once(db_session, user):
    """A webhook delivered three times. This is the whole reason the reference
    is unique per wallet."""
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_ABC123",
    )

    with pytest.raises(Conflict):
        credit(
            db_session,
            user_id=user.id,
            amount=Decimal("100.00"),
            kind=EntryKind.TOPUP,
            reference="pay_ABC123",
        )

    assert balance_of(db_session, user_id=user.id) == Decimal("100.00")


def test_two_students_may_share_a_reference(db_session, user):
    """Uniqueness is per wallet. Two people can legitimately have entries
    referencing the same order id -- a refund split, for instance -- and a
    global unique index would refuse the second for no reason."""
    other = User(email="other@example.com", hashed_password="x")
    db_session.add(other)
    db_session.flush()

    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("10.00"),
        kind=EntryKind.PROMO,
        reference="promo_launch",
    )
    credit(
        db_session,
        user_id=other.id,
        amount=Decimal("10.00"),
        kind=EntryKind.PROMO,
        reference="promo_launch",
    )

    assert balance_of(db_session, user_id=other.id) == Decimal("10.00")


@pytest.mark.parametrize("bad", [Decimal("0.00"), Decimal("-5.00")])
def test_a_credit_must_be_positive(db_session, user, bad):
    with pytest.raises(BadRequest):
        credit(
            db_session,
            user_id=user.id,
            amount=bad,
            kind=EntryKind.TOPUP,
            reference="pay_X",
        )


def test_a_float_amount_is_refused(db_session, user):
    with pytest.raises(TypeError):
        credit(
            db_session,
            user_id=user.id,
            amount=100.0,
            kind=EntryKind.TOPUP,
            reference="pay_X",
        )


# ── spending ────────────────────────────────────────────────────────────────


def test_spending_lowers_the_balance_and_records_a_negative_entry(db_session, user):
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_1",
    )

    entry = debit(
        db_session, user_id=user.id, amount=Decimal("30.00"), reference="ord_1"
    )

    assert balance_of(db_session, user_id=user.id) == Decimal("70.00")
    assert entry.amount_inr == Decimal("-30.00")
    assert entry.kind is EntryKind.SPEND


def test_spending_more_than_there_is_is_refused(db_session, user):
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("20.00"),
        kind=EntryKind.TOPUP,
        reference="pay_1",
    )

    with pytest.raises(BadRequest) as exc:
        debit(db_session, user_id=user.id, amount=Decimal("20.01"), reference="ord_1")

    assert "balance" in str(exc.value).lower()


def test_a_refused_debit_writes_no_ledger_entry(db_session, user):
    """A failed spend that left a row would make the ledger disagree with the
    balance, which is the defect this module is shaped to prevent."""
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("20.00"),
        kind=EntryKind.TOPUP,
        reference="pay_1",
    )

    with pytest.raises(BadRequest):
        debit(db_session, user_id=user.id, amount=Decimal("50.00"), reference="ord_1")

    assert db_session.query(WalletEntry).count() == 1
    assert balance_of(db_session, user_id=user.id) == Decimal("20.00")


def test_spending_exactly_the_balance_is_allowed(db_session, user):
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("20.00"),
        kind=EntryKind.TOPUP,
        reference="pay_1",
    )

    debit(db_session, user_id=user.id, amount=Decimal("20.00"), reference="ord_1")

    assert balance_of(db_session, user_id=user.id) == Decimal("0.00")


def test_spending_from_a_wallet_that_does_not_exist_is_refused(db_session, user):
    with pytest.raises(BadRequest):
        debit(db_session, user_id=user.id, amount=Decimal("1.00"), reference="ord_1")


def test_a_refused_spend_does_not_bring_a_wallet_into_existence(db_session, user):
    """Refusing early rather than creating an empty wallet and letting the
    conditional UPDATE refuse it. Both refuse; only one leaves a row behind for
    every student who ever tapped "pay with wallet" without having one."""
    with pytest.raises(BadRequest):
        debit(db_session, user_id=user.id, amount=Decimal("1.00"), reference="ord_1")

    assert db_session.query(Wallet).count() == 0


def test_a_float_spend_is_refused(db_session, user):
    """Same rule as a credit. A float has already lost precision, and the two
    paths must not disagree about that."""
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_1",
    )

    with pytest.raises(TypeError):
        debit(db_session, user_id=user.id, amount=30.0, reference="ord_1")


def test_a_debit_is_idempotent_on_its_reference(db_session, user):
    """An order paid twice by a retried request must not be charged twice."""
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_1",
    )
    debit(db_session, user_id=user.id, amount=Decimal("30.00"), reference="ord_1")

    with pytest.raises(Conflict):
        debit(db_session, user_id=user.id, amount=Decimal("30.00"), reference="ord_1")

    assert balance_of(db_session, user_id=user.id) == Decimal("70.00")


# ── the ledger is the record ────────────────────────────────────────────────


def test_the_balance_is_always_the_sum_of_the_ledger(db_session, user):
    """The invariant, over a generated history rather than one happy example.

    The old backend's ledger carried a `status`, so a sum over it depended on
    which statuses the reader thought counted -- and readers disagreed. Here
    every row is a fact and the sum is the balance.
    """
    random.seed(20260816)
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("500.00"),
        kind=EntryKind.TOPUP,
        reference="pay_seed",
    )

    for step in range(40):
        amount = Decimal(random.randrange(1, 5000)) / 100
        try:
            if random.random() < 0.5:
                debit(
                    db_session,
                    user_id=user.id,
                    amount=amount,
                    reference=f"ord_{step}",
                )
            else:
                credit(
                    db_session,
                    user_id=user.id,
                    amount=amount,
                    kind=EntryKind.TOPUP,
                    reference=f"pay_{step}",
                )
        except BadRequest:
            # Spending more than the balance is refused, which is itself part of
            # the invariant: it must leave nothing behind.
            pass

        entries = db_session.query(WalletEntry).all()
        assert sum(e.amount_inr for e in entries) == balance_of(
            db_session, user_id=user.id
        )


def test_the_balance_never_goes_negative_however_it_is_driven(db_session, user):
    random.seed(1)
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("50.00"),
        kind=EntryKind.TOPUP,
        reference="pay_seed",
    )

    for step in range(30):
        try:
            debit(
                db_session,
                user_id=user.id,
                amount=Decimal(random.randrange(1, 3000)) / 100,
                reference=f"ord_{step}",
            )
        except BadRequest:
            pass
        assert balance_of(db_session, user_id=user.id) >= Decimal("0.00")


def test_a_statement_lists_what_happened_newest_first(db_session, user):
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_1",
    )
    debit(db_session, user_id=user.id, amount=Decimal("10.00"), reference="ord_1")

    lines = statement(db_session, user_id=user.id)

    assert [line.reference for line in lines] == ["ord_1", "pay_1"]


def test_a_statement_for_an_untouched_wallet_is_empty_not_an_error(db_session, user):
    assert statement(db_session, user_id=user.id) == []


# ── the double spend ────────────────────────────────────────────────────────


@pytest.fixture
def committed_wallet(schema):
    """A really-committed wallet, for two connections to contend over.

    `db_session` runs inside an outer transaction that is rolled back, so its
    writes are invisible to a second connection -- right for ordinary tests and
    useless for this one.
    """
    engine = get_engine(schema)
    setup = Session(engine)

    user = User(email="race-wallet@example.com", hashed_password="x")
    setup.add(user)
    setup.flush()

    credit(
        setup,
        user_id=user.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_seed",
    )
    setup.commit()

    user_id = user.id
    try:
        yield user_id, engine
    finally:
        cleanup = Session(engine)
        wallet = cleanup.query(Wallet).filter_by(user_id=user_id).one_or_none()
        if wallet is not None:
            cleanup.query(WalletEntry).filter_by(wallet_id=wallet.id).delete()
            cleanup.query(Wallet).filter_by(id=wallet.id).delete()
        cleanup.query(User).filter_by(id=user_id).delete()
        cleanup.commit()
        cleanup.close()
        setup.close()


def test_two_concurrent_spends_of_one_balance_cannot_both_succeed(committed_wallet):
    """The double spend, reproduced properly.

    Read the balance, check it in Python, write it back, and two requests both
    pass the check. A conditional UPDATE makes Postgres decide instead: the
    second transaction blocks on the row until the first commits, then sees the
    real balance and fails its own condition.
    """
    user_id, engine = committed_wallet
    first, second = Session(engine), Session(engine)

    try:
        debit(first, user_id=user_id, amount=Decimal("80.00"), reference="ord_a")
        first.commit()

        with pytest.raises(BadRequest):
            debit(second, user_id=user_id, amount=Decimal("80.00"), reference="ord_b")

        assert balance_of(second, user_id=user_id) == Decimal("20.00")
    finally:
        first.close()
        second.close()


def test_the_ledger_survives_a_refused_concurrent_spend(committed_wallet):
    user_id, engine = committed_wallet
    first, second = Session(engine), Session(engine)

    try:
        debit(first, user_id=user_id, amount=Decimal("80.00"), reference="ord_a")
        first.commit()

        with pytest.raises(BadRequest):
            debit(second, user_id=user_id, amount=Decimal("80.00"), reference="ord_b")
        second.rollback()

        wallet = second.query(Wallet).filter_by(user_id=user_id).one()
        entries = second.query(WalletEntry).filter_by(wallet_id=wallet.id).all()
        assert sum(e.amount_inr for e in entries) == wallet.balance_inr
    finally:
        first.close()
        second.close()
