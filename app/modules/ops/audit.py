"""Writing down what somebody did.

Deliberately does **not** commit, and that is the whole design. The audit row
and the change it describes land in the same transaction, so a mutation that
fails cannot leave behind an entry claiming it happened, and an entry cannot
exist for a change that was rolled back. `get_db` commits once per request; this
just adds to the session.

The old backend had a helper with the same property and the same signature. What
it did not have was anything that noticed when a route forgot to call it, which
is why it covered 15 of 94 mutating routes. That gap is closed in
`tests/ops/audit_matrix.py`, not here -- a module cannot make its own callers
call it.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ops.models import AuditEntry

# What must never be written into an audit row, however it is nested. Auditing a
# credential change is important; auditing the credential is a second copy of a
# secret in a table people are allowed to read.
REDACTED = "***"
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "hashed_password",
        "key_secret",
        "razorpay_key_secret",
        "razorpay_key_secret_encrypted",
        "razorpay_webhook_secret_encrypted",
        "webhook_secret",
        "secret",
        "token",
        "refresh_token",
        "access_token",
    }
)


def scrub(value: Any) -> Any:
    """A value safe to store, with secrets replaced and Decimals made portable.

    Recursive because the shapes passed in are whatever a caller built, and a
    secret one level down is still a secret. `Decimal` becomes a string rather
    than a float -- JSON has no decimal type, and money that round-trips through
    a float is money that has lost precision in the one place people go to find
    out what it used to be.
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if key.lower() in SENSITIVE_KEYS else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_user_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
    note: str | None = None,
) -> AuditEntry:
    """Record one thing somebody did.

    `actor_user_id` is None when the system did it -- a scheduled expiry, a
    webhook settling a payment. That is a real distinction and worth keeping:
    "nobody did this" is not the same as "we failed to record who".

    `entity_id` is the **public** id. An audit trail is read by people, and
    `ksk_7f2...` is something an admin can paste into a URL.
    """
    entry = AuditEntry(
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=scrub(before) if before is not None else None,
        after=scrub(after) if after is not None else None,
        note=note,
    )
    db.add(entry)
    return entry


def entries_for(
    db: Session,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    actor_user_id: int | None = None,
    limit: int = 100,
) -> list[AuditEntry]:
    """The trail, newest first, narrowed by whatever the caller knows."""
    stmt = select(AuditEntry)
    if entity_type is not None:
        stmt = stmt.where(AuditEntry.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditEntry.entity_id == entity_id)
    if action is not None:
        stmt = stmt.where(AuditEntry.action == action)
    if actor_user_id is not None:
        stmt = stmt.where(AuditEntry.actor_user_id == actor_user_id)

    return list(
        db.execute(
            stmt.order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc()).limit(
                limit
            )
        ).scalars()
    )
