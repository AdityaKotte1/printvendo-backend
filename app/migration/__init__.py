"""Moving what has to move from the backend being replaced.

Only student money and the accounts it belongs to. Print jobs, kiosks, orders
and settlements stay behind: the new system is not a continuation of the old
one's history, and carrying rows nobody will read would mean carrying the old
schema's mistakes with them.

Only the **remaining balance**, not the transaction history. The legacy ledger
disagreed with its own stored balance, so replaying it would mean deciding per
row which of two numbers a student is owed. Requests for an old statement are
answered by hand from the retained dump -- **which therefore has to be kept**,
an operational commitment rather than a detail.

**No import of `cloud-backend`.** The legacy schema is known here as SQL text,
not as imported ORM models. That repository is deleted at cutover, and a
migration that could not run without it would stop working the day it went away.
"""

from app.migration.legacy import LegacyUser, legacy_engine, read_wallet_users
from app.migration.wallets import (
    Recipient,
    WalletMigrationReport,
    migrate_wallets,
    plan,
)

__all__ = [
    "LegacyUser",
    "Recipient",
    "WalletMigrationReport",
    "legacy_engine",
    "migrate_wallets",
    "plan",
    "read_wallet_users",
]
