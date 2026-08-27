"""Attaching legacy wallet balances to accounts in the new system.

Four decisions, all taken deliberately:

**Only the remaining balance.** No transaction history -- the legacy ledger
disagreed with its own stored balance, so replaying it would mean deciding per
row which of two numbers a student is owed. Requests for an old statement are
answered by hand from the retained dump.

**Matched by address, lowercased.** A recorded migration decision: addresses
differing only in case are the same person, and they merge onto the **oldest**
account. For a wallet that means their balances are **added together** -- which
is why the balances are summed before anything is carried, rather than carried
once per legacy row. Carrying twice would leave the student with whichever
balance happened to be read last instead of the total.

**Passwords come across.** `app.core.security` accepts `pbkdf2_sha256` and
re-hashes to bcrypt on a successful login. Nobody meets a password reset on the
morning of the cutover.

**Nothing is written unless asked.** `apply` defaults to False, because the
useful output of this is a report somebody reads before any money moves. A
migration that moved money by default is one somebody runs to see what it would
do.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.money import as_money
from app.migration.legacy import LegacyUser
from app.modules.identity import Role, User
from app.modules.identity import repository as identity_repo
from app.modules.wallet import carry_balance

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Recipient:
    """One new-system account and the money that should end up in it."""

    email: str
    full_name: str | None
    hashed_password: str | None
    is_active: bool
    balance: Decimal
    legacy_id: int
    merged_from: list[int] = field(default_factory=list)


@dataclass
class WalletMigrationReport:
    """What a person signs off before this runs for real.

    Every figure is a total that can be checked against the legacy database by
    hand, which is the only reason to produce a report at all.
    """

    accounts_created: int = 0
    accounts_matched: int = 0
    addresses_merged: int = 0
    money_carried: Decimal = Decimal("0.00")
    money_expected: Decimal = Decimal("0.00")
    wallets_credited: int = 0
    no_password: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    @property
    def needs_a_person(self) -> bool:
        """Whether anything here should stop a cutover for a conversation."""
        return bool(self.refused)

    @property
    def reconciles(self) -> bool:
        """Whether the money that moved is the money that was supposed to.

        Only meaningful after an `apply` run. The two figures differ when an
        account was refused, or when a wallet already held some of the balance
        because the migration is being re-run.
        """
        return self.money_carried == self.money_expected


def plan(legacy_users: list[LegacyUser]) -> list[Recipient]:
    """Collapse the legacy rows into one recipient per address.

    Separate from applying it so the arithmetic can be read and tested without a
    database anywhere near it -- and so the report can be produced from the same
    plan that is later carried out, rather than from a second walk that might
    disagree with it.
    """
    by_address: OrderedDict[str, Recipient] = OrderedDict()

    for legacy in legacy_users:
        key = legacy.email.strip().lower()
        existing = by_address.get(key)

        if existing is None:
            by_address[key] = Recipient(
                email=key,
                full_name=legacy.full_name,
                hashed_password=legacy.hashed_password,
                is_active=legacy.is_active,
                balance=as_money(legacy.balance),
                legacy_id=legacy.id,
            )
            continue

        # The oldest account wins for identity -- the caller reads them oldest
        # first -- and the money is added to it. A student with two spellings of
        # their address had money in both, and both were theirs.
        by_address[key] = Recipient(
            email=existing.email,
            full_name=existing.full_name or legacy.full_name,
            hashed_password=existing.hashed_password,
            is_active=existing.is_active,
            balance=as_money(existing.balance + legacy.balance),
            legacy_id=existing.legacy_id,
            merged_from=[*existing.merged_from, legacy.id],
        )

    return list(by_address.values())


def migrate_wallets(
    db: Session,
    legacy_users: list[LegacyUser],
    *,
    apply: bool = False,
) -> WalletMigrationReport:
    """Attach legacy balances to accounts here. Reports either way."""
    report = WalletMigrationReport()

    for recipient in plan(legacy_users):
        report.addresses_merged += len(recipient.merged_from)
        report.money_expected = as_money(report.money_expected + recipient.balance)

        if not recipient.hashed_password:
            # Named rather than guessed at. The account still arrives; it simply
            # cannot be signed into until the person resets their password, and
            # that was already true on the old system.
            report.no_password.append(recipient.email)

        user = identity_repo.get_by_email(db, recipient.email)
        if user is not None:
            report.accounts_matched += 1
        else:
            report.accounts_created += 1
            if not apply:
                continue
            user = User(
                email=recipient.email,
                full_name=recipient.full_name,
                hashed_password=recipient.hashed_password or "",
                is_active=recipient.is_active,
                legacy_id=recipient.legacy_id,
            )
            db.add(user)
            db.flush()
            identity_repo.grant_role(db, user.id, Role.STUDENT)
            db.flush()

        if not apply:
            continue

        try:
            carried = carry_balance(db, user_id=user.id, balance=recipient.balance)
        except Exception as exc:  # noqa: BLE001
            # One bad account must not take the cutover down with it. Named, so
            # it is dealt with rather than discovered.
            logger.error("could not carry %s: %s", recipient.email, exc)
            report.refused.append(f"{recipient.email}: {exc}")
            continue

        report.money_carried = as_money(report.money_carried + carried.carried)
        if carried.wrote_entry:
            report.wallets_credited += 1

    return report
