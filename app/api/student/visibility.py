"""Whether a student may use a kiosk at all.

One predicate, because there were three. The listing asked
`LIVE and can_take_payment`; the order route asked nothing and went straight to
`get_kiosk`; and `place_order` asked the payment gate. So a shop could be absent
from the map and still take an order, and `is_selling` -- the function actually
named for this question -- was used by none of them and only ever rendered as a
flag on the owner and admin screens.

**A shop with no machine reporting in is shut.** That is the part that was
missing everywhere: a kiosk whose Pi has lost power, lost wifi, or been carried
to another building stayed on the map, took payment, and queued work nothing
would ever collect. Students paid and stood waiting; operators found out from
the refund list.

Derived from the last heartbeat rather than the device's `status` column, for
the reason `is_online` gives: a machine whose power was pulled never gets to say
it is going away. The window is `HEARTBEAT_WINDOW` (five minutes) against a beat
every sixteen seconds, so a shop has to miss roughly twenty in a row before it
disappears -- long enough that a flaky connection does not flicker the map, short
enough that nobody pays a shop that went dark while they were choosing options.
"""

from sqlalchemy.orm import Session

from app.modules.kiosks import Kiosk, OnboardingStage, device_of, is_online
from app.modules.payments import can_take_payment


def student_may_order(db: Session, kiosk: Kiosk) -> bool:
    """Whether a student may order at this kiosk right now."""
    if kiosk.onboarding_stage is not OnboardingStage.LIVE:
        return False
    if not can_take_payment(db, kiosk):
        return False
    return is_online(device_of(db, kiosk))
