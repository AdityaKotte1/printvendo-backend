"""Reading the old database, in SQL, without importing the old code.

The legacy schema is written out here as queries rather than reached through
`cloud-backend`'s ORM. That is not squeamishness: that repository is deleted at
cutover, its `Base` is a different declarative base, and importing it would give
this migration a dependency that stops existing the moment the job is done.

**Read-only.** Nothing here writes to the legacy database. The dump is taken
during a maintenance window and is the only copy of those rows; a migration that
could modify it is one that could destroy the thing it is reading.

**The ledger is deliberately not read.** Only the closing balance comes across
-- see `wallet.imports` for why. A student who wants their old statement asks,
and somebody answers from the dump, which therefore has to be kept.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class LegacyUser:
    """A legacy account with money left in its wallet.

    `hashed_password` comes across so people sign in as they did yesterday:
    `app.core.security` accepts `pbkdf2_sha256` and re-hashes to bcrypt on a
    successful login, which exists for exactly this.
    """

    id: int
    email: str
    full_name: str | None
    hashed_password: str | None
    is_active: bool
    created_at: datetime
    balance: Decimal


def legacy_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


# Only accounts with money left. One with a zero balance has nothing to carry,
# and creating it would import a dormant row so that somebody who never used the
# wallet does not have to register again -- which they can.
#
# Oldest first, because case-duplicate addresses merge onto the oldest account
# and the caller relies on seeing that one first.
_USERS = text(
    """
    select u.id, u.email, u.full_name, u.hashed_password, u.is_active,
           u.created_at, w.balance
    from users u
    join wallets w on w.user_id = u.id
    where w.balance > 0
    order by u.created_at, u.id
    """
)


def read_wallet_users(engine: Engine) -> list[LegacyUser]:
    """Every legacy account with money left in its wallet."""
    with engine.connect() as connection:
        return [
            LegacyUser(
                id=row["id"],
                email=row["email"],
                full_name=row["full_name"],
                hashed_password=row["hashed_password"],
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
                balance=Decimal(str(row["balance"])),
            )
            for row in connection.execute(_USERS).mappings()
        ]
