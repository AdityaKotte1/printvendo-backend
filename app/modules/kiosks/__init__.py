"""The kiosks bounded context.

Import from here, never from the submodules' internals. Entity types are part of
the contract because callers must annotate what the services return; the
*tables* are not, and importing app.modules.kiosks.models directly from the api
layer breaks the import contracts.
"""

from app.modules.kiosks.devices import (
    ENROLMENT_LIFETIME,
    HEARTBEAT_WINDOW,
    IssuedDevice,
    IssuedEnrolment,
    authenticate_device,
    device_of,
    is_online,
    issue_enrolment_code,
    record_heartbeat,
    register_device,
    revoke_device,
    rotate_token,
)
from app.modules.kiosks.enums import (
    AssignmentRole,
    DeviceStatus,
    KioskType,
    OnboardingStage,
)
from app.modules.kiosks.models import Kiosk, KioskDevice, KioskPaper
from app.modules.kiosks.onboarding import (
    BillingCheck,
    PlatformOnlyBilling,
    is_selling,
    move_to,
    reconcile_billing_state,
)
from app.modules.kiosks.paper import consume_paper, sheets_remaining
from app.modules.kiosks.pricing import (
    UNBOUNDED as UnboundedBand,
)
from app.modules.kiosks.pricing import (
    BandSource,
    PriceBand,
    effective_prices,
    read_pricing,
    set_pricing,
)
from app.modules.kiosks.registry import (
    by_legacy_id,
    change_type,
    create_kiosk,
    rename_kiosk,
    set_accepts_wallet,
    set_active,
    set_location,
)
from app.modules.kiosks.scope import Scope, kiosk_scope, system_scope
from app.modules.kiosks.staffing import (
    accept_invite,
    invite_staff,
    list_staff,
    revoke_invite,
    unassign,
)

__all__ = [
    "ENROLMENT_LIFETIME",
    "HEARTBEAT_WINDOW",
    "AssignmentRole",
    "BandSource",
    "BillingCheck",
    "DeviceStatus",
    "IssuedDevice",
    "IssuedEnrolment",
    "Kiosk",
    "KioskDevice",
    "KioskPaper",
    "KioskType",
    "OnboardingStage",
    "PlatformOnlyBilling",
    "PriceBand",
    "UnboundedBand",
    "Scope",
    "authenticate_device",
    "consume_paper",
    "device_of",
    "effective_prices",
    "is_online",
    "is_selling",
    "issue_enrolment_code",
    "kiosk_scope",
    "system_scope",
    "move_to",
    "read_pricing",
    "reconcile_billing_state",
    "record_heartbeat",
    "register_device",
    "revoke_device",
    "rotate_token",
    "set_pricing",
    "accept_invite",
    "by_legacy_id",
    "change_type",
    "create_kiosk",
    "invite_staff",
    "list_staff",
    "rename_kiosk",
    "revoke_invite",
    "set_accepts_wallet",
    "set_active",
    "set_location",
    "unassign",
    "sheets_remaining",
]
