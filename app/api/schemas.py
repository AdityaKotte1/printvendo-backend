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


# ── devices ─────────────────────────────────────────────────────────────────


class DeviceRegisterRequest(BaseModel):
    """What an agent sends when it is first installed at a kiosk.

    The enrolment code is the credential. Notice what is *absent*: no kiosk id.
    Which kiosk this machine belongs to is decided by the code, not asserted by
    the caller.
    """

    enrolment_code: str
    device_key: str | None = None
    agent_version: str | None = None
    capabilities: str | None = None


class DeviceRegisterResponse(BaseModel):
    """The one and only time the device token is readable."""

    device_key: str
    token: str
    kiosk_id: str
    kiosk_name: str


class DeviceHeartbeatRequest(BaseModel):
    agent_version: str | None = None
    status: str | None = None


class DeviceHeartbeatResponse(BaseModel):
    kiosk_id: str
    kiosk_name: str
    queue_depth: int
    sheets_remaining: int


class DeviceTaskResponse(BaseModel):
    """A print task, resolved.

    Every value the printer needs is here, already decided. The agent maps them
    to CUPS flags and nothing more -- it does not parse an options blob, apply
    defaults, or work anything out. That is what stopped the price charged, the
    paper deducted and the pages printed from being three different opinions.
    """

    task_id: str
    document_id: str
    filename: str
    file_url: str
    page_count: int | None
    copies: int
    duplex: bool
    colour: bool
    page_range: str | None
    expected_sheets: int
    lease_expires_at: datetime | None


class DeviceTaskStatusRequest(BaseModel):
    state: str
    # CUPS `job-media-sheets-completed`. None means the agent could not tell,
    # which is a different thing from zero.
    sheets_used: int | None = None
    error_code: str | None = None
    error_message: str | None = None


class DeviceStatusResponse(BaseModel):
    """What an owner sees about the machine in their shop."""

    registered: bool
    device_key: str | None
    status: str | None
    agent_version: str | None
    last_heartbeat_at: datetime | None
    online: bool


class EnrolmentCodeResponse(BaseModel):
    code: str
    expires_at: datetime
