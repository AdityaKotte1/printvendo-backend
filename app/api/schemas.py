"""Response shapes shared across audiences.

Two rules encoded here rather than trusted to each handler:

* **A refiller never sees money.** `RefillerKioskResponse` has no price fields
  at all, so the question "did I remember to strip pricing?" cannot be answered
  wrongly -- there is nothing to strip.
* **No response carries a database row id.** Every identifier is the opaque
  public one.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class PaperResponse(BaseModel):
    capacity: int
    sheets_remaining: int


class PricesResponse(BaseModel):
    bw_single: Decimal
    bw_double: Decimal
    color_single: Decimal
    color_double: Decimal


class PriceBandResponse(BaseModel):
    floor_bw: Decimal | None
    ceiling_bw: Decimal | None
    floor_color: Decimal | None
    ceiling_color: Decimal | None


class PricingResponse(BaseModel):
    """Prices and the band together.

    One payload on purpose: a client that fetches limits separately will
    eventually show an owner a limit that no longer applies.
    """

    prices: PricesResponse
    band: PriceBandResponse


class OwnerKioskResponse(BaseModel):
    id: str
    name: str
    kiosk_type: str
    onboarding_stage: str
    onboarding_note: str | None
    is_active: bool
    is_selling: bool
    accepts_wallet: bool
    location_description: str | None
    paper: PaperResponse


class RefillerKioskResponse(BaseModel):
    """What someone who only refills paper is allowed to see.

    Deliberately missing: prices, earnings, wallet settings, onboarding notes,
    and anything about students. A refiller needs to know where the machine is
    and how much paper is in it.
    """

    id: str
    name: str
    location_description: str | None
    is_active: bool
    paper: PaperResponse


class RefillLogResponse(BaseModel):
    sheets_added: int
    capacity_at_change: int
    used_before_change: int
    note: str | None
    created_at: datetime


class StaffMemberResponse(BaseModel):
    """Someone attached to a kiosk.

    Carries a name and email because an owner has to be able to tell their own
    staff apart -- but only ever for people who accepted an invitation to *this*
    kiosk, which is what stops it being a directory of everyone on the platform.
    """

    user_id: str
    email: str
    full_name: str | None
    role: str


class StatusChangeRequest(BaseModel):
    stage: str


class PaperUpdateRequest(BaseModel):
    capacity: int | None = None
    sheets_left: int | None = None
    note: str | None = None


class PricingUpdateRequest(BaseModel):
    bw_single: Decimal | None = None
    bw_double: Decimal | None = None
    color_single: Decimal | None = None
    color_double: Decimal | None = None


class InviteStaffRequest(BaseModel):
    email: str
    role: str = "refiller"


class AcceptInviteRequest(BaseModel):
    token: str
