"""Carrying a wallet balance across at cutover.

Only the closing balance moves. The legacy `wallet_ledger` disagreed with its
own `wallets.balance` -- its `status` column is how the two came apart -- so
replaying it would mean deciding per row which of two numbers a student is
owed. The balance is what they see in the app today, so it is what they get; a
student who wants their old statement asks, and somebody answers from the
retained dump.

Two properties carry this file. The money arrives as a **ledger entry** rather
than as a bare column, so it is traceable like every other rupee here. And
running it **twice** does nothing the second time, because a cutover gets
interrupted and restarted.
"""

from decimal import Decimal

import pytest

from app.core.errors import BadRequest
from app.modules.identity.models import User
from app.modules.wallet import EntryKind, balance_of, debit, statement
from app.modules.wallet.imports import carry_balance


@pytest.fixture
def student(db_session) -> User:
    user = User(email="carried.over@university.edu", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


# ── the money arrives ───────────────────────────────────────────────────────


def test_the_balance_comes_across(db_session, student):
    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    assert balance_of(db_session, user_id=student.id) == Decimal("380.00")


def test_it_arrives_as_a_ledger_entry_not_a_bare_number(db_session, student):
    """`balance_inr` is a column so a conditional UPDATE has something to be
    conditional on, but the ledger is what a dispute is settled from. A balance
    with no entry behind it would be the first rupee in this system that cannot
    be traced."""
    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    entries = statement(db_session, user_id=student.id)
    assert len(entries) == 1
    assert entries[0].kind is EntryKind.ADJUSTMENT
    assert entries[0].amount_inr == Decimal("380.00")


def test_the_entry_says_where_the_money_came_from(db_session, student):
    """"What is this ₹380" has to have an answer, for the student and for
    whoever they ask."""
    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    note = statement(db_session, user_id=student.id)[0].note or ""
    assert "carried over" in note.lower()
    assert "previous system" in note.lower()


def test_the_module_invariant_holds_for_carried_money(db_session, student):
    """`sum(entries) == balance` is asserted elsewhere as a property of money
    this system moved. It has to be true of money it was handed, too."""
    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    entries = statement(db_session, user_id=student.id)
    assert sum(entry.amount_inr for entry in entries) == balance_of(
        db_session, user_id=student.id
    )


# ── running it twice ───────────────────────────────────────────────────────


def test_carrying_the_same_balance_twice_does_not_double_it(db_session, student):
    """Why the gap is measured against the balance that is there rather than
    assumed to be zero. A cutover gets re-run."""
    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    again = carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    assert again.wrote_entry is False
    assert balance_of(db_session, user_id=student.id) == Decimal("380.00")
    assert len(statement(db_session, user_id=student.id)) == 1


def test_a_corrected_balance_moves_only_the_difference(db_session, student):
    """A second dump says the student has 420, not 380. They end on 420, and the
    40 is its own traceable entry rather than a silent overwrite."""
    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    second = carry_balance(db_session, user_id=student.id, balance=Decimal("420.00"))

    assert second.carried == Decimal("40.00")
    assert balance_of(db_session, user_id=student.id) == Decimal("420.00")
    assert len(statement(db_session, user_id=student.id)) == 2


def test_money_spent_after_cutover_is_topped_back_up_only_to_the_legacy_figure(
    db_session, student
):
    """The case that makes measuring the gap load-bearing rather than tidy.

    The student has already spent some of the carried money on the new system.
    A re-run measures the gap and restores them *to the legacy figure* -- which
    is the honest reading of "carry the balance over" being run twice, and is
    why a cutover must not be re-run after students start spending. Stated here
    so the consequence is visible rather than discovered.
    """
    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))
    debit(db_session, user_id=student.id, amount=Decimal("80.00"), reference="spent")
    assert balance_of(db_session, user_id=student.id) == Decimal("300.00")

    carry_balance(db_session, user_id=student.id, balance=Decimal("380.00"))

    assert balance_of(db_session, user_id=student.id) == Decimal("380.00")


# ── refusals and the empty case ────────────────────────────────────────────


def test_a_negative_balance_is_refused(db_session, student):
    """A wallet cannot owe us money. A negative figure is bad legacy data and a
    person has to look at it rather than have it quietly become a debt."""
    with pytest.raises(BadRequest) as raised:
        carry_balance(db_session, user_id=student.id, balance=Decimal("-15.00"))

    assert "negative balance" in str(raised.value.detail)


def test_nothing_to_carry_writes_nothing(db_session, student):
    """A zero balance must not leave a cosmetic zero-rupee row in a statement."""
    report = carry_balance(db_session, user_id=student.id, balance=Decimal("0.00"))

    assert report.wrote_entry is False
    assert statement(db_session, user_id=student.id) == []
