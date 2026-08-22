"""The ops bounded context.

What was done (audit) and what needs attention (alerts). Both existed in the
backend being replaced and neither was in the path: the audit helper was called
from 15 of 94 mutating routes, and the notifications table's admin flag was set
by one code path and filtered on by none.

Ops depends on no other bounded context. It records *about* things -- a kiosk, a
payment, an owner -- but only ever by public id and type, never by importing
them. That is what lets any module be audited without ops knowing it exists.
"""

from app.modules.ops.alerts import (
    open_alerts,
    raise_alert,
    resolve,
    resolve_by_key,
)
from app.modules.ops.audit import entries_for, record, scrub
from app.modules.ops.models import AdminAlert, AlertSeverity, AuditEntry

__all__ = [
    "AdminAlert",
    "AlertSeverity",
    "AuditEntry",
    "entries_for",
    "open_alerts",
    "raise_alert",
    "record",
    "resolve",
    "resolve_by_key",
    "scrub",
]
