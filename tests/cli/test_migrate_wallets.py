"""The migration command, against a real legacy-shaped database.

The use case is tested with constructed inputs elsewhere. What is tested here is
the part nothing else covers: **the SQL**. `read_wallet_users` names tables and
columns that exist only in the old system, and a typo in one of them is a
cutover that fails at the worst possible moment with nobody able to say why.

So this builds the two legacy tables for real -- `users` and `wallets`, in their
old shape -- puts rows in them, and runs the reader against them. It is the only
place the query is exercised at all.
"""

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.migration import legacy_engine, migrate_wallets, read_wallet_users
from app.modules.identity import repository as identity_repo
from app.modules.wallet import balance_of

# The old schema, only as much of it as the reader touches. Written out rather
# than imported: `cloud-backend` is deleted at cutover, and a test that needed
# it would stop running the day it went.
_LEGACY_DDL = (
    """
    create table legacy_users (
        id serial primary key,
        email varchar not null,
        full_name varchar,
        hashed_password varchar,
        is_active boolean not null default true,
        created_at timestamp not null default now()
    )
    """,
    """
    create table legacy_wallets (
        id serial primary key,
        user_id integer not null references legacy_users(id),
        balance numeric(10, 2) not null default 0.00
    )
    """,
)


@pytest.fixture
def legacy_db(postgres_url: str):
    """A legacy-shaped database, in its own schema so it cannot be confused
    with the one under migration.

    The reader's queries name `users` and `wallets`, so the schema is put first
    on the search path rather than the tables being renamed -- that way the SQL
    under test is the SQL that will run in production, character for character.
    """
    engine = legacy_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(text("drop schema if exists legacy cascade"))
        connection.execute(text("create schema legacy"))
        for statement in _LEGACY_DDL:
            connection.execute(
                text(statement.replace("legacy_users", "legacy.users").replace(
                    "legacy_wallets", "legacy.wallets"
                ))
            )
    engine.dispose()

    scoped = legacy_engine(f"{postgres_url}?options=-csearch_path%3Dlegacy,public")
    yield scoped
    scoped.dispose()

    engine = legacy_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(text("drop schema if exists legacy cascade"))
    engine.dispose()


def _add(engine, email: str, balance: str, *, name="Ravi Kumar", password="pbkdf2$x"):
    with engine.begin() as connection:
        user_id = connection.execute(
            text(
                "insert into legacy.users (email, full_name, hashed_password) "
                "values (:e, :n, :p) returning id"
            ),
            {"e": email, "n": name, "p": password},
        ).scalar_one()
        connection.execute(
            text(
                "insert into legacy.wallets (user_id, balance) values (:u, :b)"
            ),
            {"u": user_id, "b": Decimal(balance)},
        )
    return user_id


# ── the SQL ─────────────────────────────────────────────────────────────────


def test_the_reader_finds_an_account_with_money(legacy_db):
    """The query, run for real. Every column it names has to exist."""
    _add(legacy_db, "a@x.edu", "40.00")

    found = read_wallet_users(legacy_db)

    assert [(u.email, u.balance) for u in found] == [("a@x.edu", Decimal("40.00"))]


def test_the_reader_carries_the_fields_the_migration_needs(legacy_db):
    _add(legacy_db, "a@x.edu", "40.00", name="Ravi Kumar", password="pbkdf2$abc")

    user = read_wallet_users(legacy_db)[0]

    assert user.full_name == "Ravi Kumar"
    assert user.hashed_password == "pbkdf2$abc"
    assert user.is_active is True
    assert user.created_at is not None


def test_an_empty_wallet_is_not_carried(legacy_db):
    """Nothing to move, and creating the account would import a dormant row so
    that somebody who never used the wallet need not register again."""
    _add(legacy_db, "broke@x.edu", "0.00")

    assert read_wallet_users(legacy_db) == []


def test_an_account_with_no_wallet_row_is_not_carried(legacy_db):
    with legacy_db.begin() as connection:
        connection.execute(
            text("insert into legacy.users (email) values ('nowallet@x.edu')")
        )

    assert read_wallet_users(legacy_db) == []


def test_accounts_come_back_oldest_first(legacy_db):
    """The merge rule depends on it: case-duplicates fold onto the oldest, and
    the planner keeps whichever it sees first."""
    _add(legacy_db, "first@x.edu", "10.00")
    _add(legacy_db, "second@x.edu", "20.00")

    found = read_wallet_users(legacy_db)

    assert [u.email for u in found] == ["first@x.edu", "second@x.edu"]


# ── the whole way through ───────────────────────────────────────────────────


def test_money_travels_from_the_old_database_to_a_wallet_here(legacy_db, db_session):
    """The end to end: real legacy tables, the real reader, the real use case."""
    _add(legacy_db, "Ravi@X.edu", "40.00")
    _add(legacy_db, "ravi@x.edu", "15.50")

    report = migrate_wallets(db_session, read_wallet_users(legacy_db), apply=True)

    assert report.money_carried == Decimal("55.50")
    user = identity_repo.get_by_email(db_session, "ravi@x.edu")
    assert balance_of(db_session, user_id=user.id) == Decimal("55.50")


def test_a_dry_run_against_the_old_database_writes_nothing(legacy_db, db_session):
    _add(legacy_db, "a@x.edu", "40.00")

    report = migrate_wallets(db_session, read_wallet_users(legacy_db))

    assert report.money_expected == Decimal("40.00")
    assert identity_repo.get_by_email(db_session, "a@x.edu") is None
