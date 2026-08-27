"""Attaching legacy wallet balances to accounts here.

The scope is deliberately small: remaining balance only, matched by email.
Everything else about the old system stays behind, and a request for an old
statement is answered by hand from the retained dump.

Two things carry this file. The **merge arithmetic**, because a student with two
spellings of their address had money in both and both were theirs -- and getting
it wrong loses somebody's money rather than producing a wrong report. And the
**dry run**, because nothing here should move money until a person has read what
it intends to move.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.migration import LegacyUser, migrate_wallets, plan
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.wallet import balance_of, statement

EARLY = datetime(2025, 3, 1, 9, 0, tzinfo=UTC)


def legacy(
    email: str,
    balance: str,
    *,
    user_id: int = 1,
    at: datetime = EARLY,
    name: str | None = "Ravi Kumar",
    password: str | None = "pbkdf2_sha256$legacy",
    active: bool = True,
) -> LegacyUser:
    return LegacyUser(
        id=user_id,
        email=email,
        full_name=name,
        hashed_password=password,
        is_active=active,
        created_at=at,
        balance=Decimal(balance),
    )


# ── the plan, which is arithmetic and needs no database ────────────────────


def test_one_account_per_address():
    recipients = plan([legacy("a@x.edu", "40.00", user_id=1)])

    assert [(r.email, r.balance) for r in recipients] == [("a@x.edu", Decimal("40.00"))]


def test_an_address_is_lowercased():
    """A recorded migration decision: addresses differing only in case are the
    same person. The new system stores one spelling."""
    recipients = plan([legacy("Ravi@X.edu", "40.00")])

    assert recipients[0].email == "ravi@x.edu"


def test_case_duplicates_have_their_money_added_together():
    """The arithmetic that matters. Two spellings, money in both, and both of it
    is the student's -- so carrying one balance and discarding the other would
    take money off somebody."""
    recipients = plan(
        [
            legacy("Ravi@x.edu", "40.00", user_id=1, at=EARLY),
            legacy("ravi@x.edu", "15.50", user_id=2, at=EARLY + timedelta(days=30)),
        ]
    )

    assert len(recipients) == 1
    assert recipients[0].balance == Decimal("55.50")


def test_the_oldest_account_is_the_one_kept():
    """Also recorded. The older account is the one with the history behind it,
    so it is the identity that survives and the newer one merges into it."""
    recipients = plan(
        [
            legacy("Ravi@x.edu", "40.00", user_id=1, at=EARLY),
            legacy("ravi@x.edu", "15.50", user_id=2, at=EARLY + timedelta(days=30)),
        ]
    )

    assert recipients[0].legacy_id == 1
    assert recipients[0].merged_from == [2]


def test_a_name_is_taken_from_the_duplicate_when_the_oldest_has_none():
    """Nothing important, but losing a name somebody typed for no reason is
    worse than the one line it takes to keep it."""
    recipients = plan(
        [
            legacy("Ravi@x.edu", "40.00", user_id=1, name=None),
            legacy("ravi@x.edu", "15.50", user_id=2, name="Ravi Kumar"),
        ]
    )

    assert recipients[0].full_name == "Ravi Kumar"


# ── the dry run ────────────────────────────────────────────────────────────


def test_nothing_is_written_without_being_asked(db_session):
    """The useful output is the report. A migration that moved money by default
    is one somebody runs to see what it would do."""
    report = migrate_wallets(db_session, [legacy("a@x.edu", "40.00")])

    assert report.accounts_created == 1
    assert report.money_expected == Decimal("40.00")
    assert report.money_carried == Decimal("0.00")
    assert identity_repo.get_by_email(db_session, "a@x.edu") is None


def test_a_dry_run_still_counts_the_money_it_would_move(db_session):
    report = migrate_wallets(
        db_session,
        [legacy("a@x.edu", "40.00", user_id=1), legacy("b@x.edu", "60.00", user_id=2)],
    )

    assert report.money_expected == Decimal("100.00")


# ── applying it ────────────────────────────────────────────────────────────


def test_the_account_is_created_and_the_money_arrives(db_session):
    migrate_wallets(db_session, [legacy("a@x.edu", "40.00")], apply=True)

    user = identity_repo.get_by_email(db_session, "a@x.edu")
    assert user is not None
    assert balance_of(db_session, user_id=user.id) == Decimal("40.00")


def test_a_carried_account_is_a_student(db_session):
    migrate_wallets(db_session, [legacy("a@x.edu", "40.00")], apply=True)

    user = identity_repo.get_by_email(db_session, "a@x.edu")
    assert Role.STUDENT in identity_repo.roles_of(db_session, user.id)


def test_the_password_comes_across_so_a_login_still_works(db_session):
    """`app.core.security` accepts pbkdf2 and re-hashes to bcrypt on a
    successful login. Nobody meets a password reset on cutover morning."""
    migrate_wallets(
        db_session, [legacy("a@x.edu", "40.00", password="pbkdf2_sha256$abc")], apply=True
    )

    user = identity_repo.get_by_email(db_session, "a@x.edu")
    assert user.hashed_password == "pbkdf2_sha256$abc"


def test_the_legacy_id_is_recorded_on_the_account(db_session):
    """So a support request naming an old id can be answered without guessing."""
    migrate_wallets(db_session, [legacy("a@x.edu", "40.00", user_id=4242)], apply=True)

    user = identity_repo.get_by_email(db_session, "a@x.edu")
    assert user.legacy_id == 4242


def test_merged_money_lands_once_and_in_full(db_session):
    """The end-to-end version of the arithmetic above: the student gets the
    total, in one wallet, and only once."""
    migrate_wallets(
        db_session,
        [
            legacy("Ravi@x.edu", "40.00", user_id=1, at=EARLY),
            legacy("ravi@x.edu", "15.50", user_id=2, at=EARLY + timedelta(days=30)),
        ],
        apply=True,
    )

    user = identity_repo.get_by_email(db_session, "ravi@x.edu")
    assert balance_of(db_session, user_id=user.id) == Decimal("55.50")
    assert len(statement(db_session, user_id=user.id)) == 1


def test_an_account_that_already_exists_here_is_used_rather_than_duplicated(db_session):
    """Somebody may have registered on the new system before cutover. Their
    money still has to find them, and a second account with the same address
    would not be possible anyway."""
    existing = User(email="a@x.edu", hashed_password="already-here")
    db_session.add(existing)
    db_session.flush()

    report = migrate_wallets(db_session, [legacy("a@x.edu", "40.00")], apply=True)

    assert report.accounts_matched == 1
    assert report.accounts_created == 0
    assert balance_of(db_session, user_id=existing.id) == Decimal("40.00")


def test_an_existing_password_is_not_overwritten(db_session):
    """They have signed in here already. Replacing their password with the old
    system's hash would sign them out of the one they are using."""
    existing = User(email="a@x.edu", hashed_password="bcrypt$current")
    db_session.add(existing)
    db_session.flush()

    migrate_wallets(db_session, [legacy("a@x.edu", "40.00")], apply=True)

    assert existing.hashed_password == "bcrypt$current"


def test_the_money_that_moved_is_the_money_that_was_meant_to(db_session):
    """The one figure a person checks the whole run against."""
    report = migrate_wallets(
        db_session,
        [legacy("a@x.edu", "40.00", user_id=1), legacy("b@x.edu", "60.50", user_id=2)],
        apply=True,
    )

    assert report.money_carried == Decimal("100.50")
    assert report.reconciles is True
    assert report.wallets_credited == 2


def test_running_it_twice_does_not_double_anybody(db_session):
    """A cutover gets interrupted and restarted."""
    rows = [legacy("a@x.edu", "40.00")]
    migrate_wallets(db_session, rows, apply=True)

    second = migrate_wallets(db_session, rows, apply=True)

    user = identity_repo.get_by_email(db_session, "a@x.edu")
    assert balance_of(db_session, user_id=user.id) == Decimal("40.00")
    assert second.wallets_credited == 0


# ── what needs a person ────────────────────────────────────────────────────


def test_an_account_with_no_password_is_named(db_session):
    """It still arrives -- it simply cannot be signed into until the person
    resets, which was already true on the old system."""
    report = migrate_wallets(
        db_session, [legacy("a@x.edu", "40.00", password=None)], apply=True
    )

    assert report.no_password == ["a@x.edu"]
    assert balance_of(
        db_session, user_id=identity_repo.get_by_email(db_session, "a@x.edu").id
    ) == Decimal("40.00")


def test_one_bad_account_does_not_take_the_cutover_down(db_session):
    """A negative legacy balance is refused by the wallet. The run must carry
    on and name it, rather than stopping halfway through a money migration."""
    report = migrate_wallets(
        db_session,
        [
            legacy("bad@x.edu", "-5.00", user_id=1),
            legacy("good@x.edu", "40.00", user_id=2),
        ],
        apply=True,
    )

    assert len(report.refused) == 1
    assert "bad@x.edu" in report.refused[0]
    assert report.needs_a_person is True

    good = identity_repo.get_by_email(db_session, "good@x.edu")
    assert balance_of(db_session, user_id=good.id) == Decimal("40.00")


def test_a_clean_run_needs_nobody(db_session):
    report = migrate_wallets(db_session, [legacy("a@x.edu", "40.00")], apply=True)

    assert report.needs_a_person is False


def test_no_legacy_wallets_is_not_an_error(db_session):
    report = migrate_wallets(db_session, [], apply=True)

    assert report.money_expected == Decimal("0.00")
    assert report.needs_a_person is False
