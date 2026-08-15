"""The kiosks bounded context.

Import from here, never from the submodules' internals. Entity types are part of
the contract because callers must annotate what the services return; the
*tables* are not, and importing app.modules.kiosks.models directly from the api
layer breaks the import contracts.
"""

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
from app.modules.kiosks.pricing import (
    BandSource,
    PlatformBand,
    PriceBand,
    effective_prices,
    read_pricing,
    set_pricing,
)
from app.modules.kiosks.scope import Scope, kiosk_scope

__all__ = [
    "AssignmentRole",
    "BandSource",
    "BillingCheck",
    "DeviceStatus",
    "Kiosk",
    "KioskDevice",
    "KioskPaper",
    "KioskType",
    "OnboardingStage",
    "PlatformBand",
    "PlatformOnlyBilling",
    "PriceBand",
    "Scope",
    "effective_prices",
    "is_selling",
    "kiosk_scope",
    "move_to",
    "read_pricing",
    "reconcile_billing_state",
    "set_pricing",
]
