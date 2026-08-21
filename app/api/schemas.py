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
from typing import Literal

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


class DocumentResponse(BaseModel):
    """A file a student uploaded.

    No price. What a print costs depends on the kiosk it goes to and the options
    chosen, and that calculation lives in one place -- the order. A price here
    would be a second opinion, and the old backend's two opinions are why a
    student could be charged for one thing and handed another.
    """

    id: str
    filename: str
    page_count: int | None
    byte_size: int | None
    state: str
    created_at: datetime


# ── kiosks a student may print at ───────────────────────────────────────────


class StudentKioskResponse(BaseModel):
    """A shop a student can send a job to.

    Prices are here because a student decides with them. Nothing about the
    owner is: not their name, not their id, not whose Razorpay collects. A
    student has no business knowing which shops share an owner, and a type that
    has no field for it cannot leak it by accident.
    """

    id: str
    name: str
    accepts_wallet: bool
    is_out_of_paper: bool
    sheets_remaining: int
    price_bw_single: Decimal
    price_bw_double: Decimal
    price_color_single: Decimal
    price_color_double: Decimal


# ── orders ──────────────────────────────────────────────────────────────────


class OrderLineRequest(BaseModel):
    """One document, printed one way."""

    document_id: str
    colour: bool = False
    duplex: bool = False
    copies: int = 1
    page_range: str | None = None


class PlaceOrderRequest(BaseModel):
    kiosk_id: str
    payment_method: Literal["wallet", "gateway"]
    items: list[OrderLineRequest]


class OrderItemResponse(BaseModel):
    document_id: str | None
    filename: str | None
    kind: str
    colour: bool
    duplex: bool
    copies: int
    page_range: str | None
    page_count: int
    sheets: int
    amount_inr: Decimal


class OrderResponse(BaseModel):
    """What the student owes and what state it is in.

    `total_inr` is the server's number and the only one ever charged. The web
    app's own estimate stays an estimate -- the old backend let a client-side
    price reach the gateway.
    """

    id: str
    kiosk_id: str
    state: str
    payment_method: str | None
    subtotal_inr: Decimal
    fee_inr: Decimal
    total_inr: Decimal
    expires_at: datetime | None
    paid_at: datetime | None
    refunded_at: datetime | None
    created_at: datetime
    items: list[OrderItemResponse]


class CheckoutResponse(BaseModel):
    """Everything the browser needs to open Razorpay, and nothing more.

    `key_id` is not a secret -- it is in the checkout the student sees anyway.
    The key *secret* has no field here and never leaves the server.
    """

    razorpay_order_id: str
    razorpay_key_id: str
    amount_inr: Decimal
    order_id: str


class VerifyPaymentRequest(BaseModel):
    """The browser handing back what Razorpay gave it.

    Verified against the key that opened the order. A callback that does not
    verify changes nothing at all, so a forged one cannot advance an order
    towards being printed.
    """

    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


# ── wallet ──────────────────────────────────────────────────────────────────


class WalletResponse(BaseModel):
    balance_inr: Decimal


class WalletEntryResponse(BaseModel):
    id: str
    kind: str
    amount_inr: Decimal
    balance_after_inr: Decimal
    note: str | None
    created_at: datetime


class TopUpRequest(BaseModel):
    amount_inr: Decimal


# ── an owner's payment configuration ────────────────────────────────────────


class PaymentConfigResponse(BaseModel):
    """What is configured, masked.

    There is no field for a key secret, and that is the mechanism rather than a
    reminder: a response type with nowhere to put a secret cannot leak one, however
    the handler is later edited.
    """

    is_configured: bool
    key_id_masked: str | None
    configured_at: datetime | None
    # True when nothing is set yet, or an admin has approved a change that has
    # not been used. One boolean so the form and the button cannot disagree.
    can_update: bool


class SetPaymentKeysRequest(BaseModel):
    key_id: str
    key_secret: str


class SetWebhookSecretRequest(BaseModel):
    # Razorpay's webhook signing secret, which they set per webhook rather than
    # per key -- a different value from the API key secret above.
    webhook_secret: str


class WebhookEndpointResponse(BaseModel):
    """The URL an owner registers in their own Razorpay dashboard."""

    url: str
    events: list[str]


# ── what an owner is shown ──────────────────────────────────────────────────


class EarningsResponse(BaseModel):
    """Money in, money back, and the difference.

    `net_inr` may be negative -- refunds today against takings from last week.
    Reported rather than clamped: the old backend wrapped it in max(0, ...),
    which hid the one case somebody has to act on.
    """

    gross_inr: Decimal
    refunded_inr: Decimal
    net_inr: Decimal
    order_count: int


class KioskEarningsResponse(BaseModel):
    kiosk_id: str
    kiosk_name: str
    earnings: EarningsResponse


class OwnerOrderResponse(BaseModel):
    """One job printed at an owner's shop.

    No name, no email, no user id -- absent rather than stripped. A shop owner
    has a legitimate interest in what was printed and what it cost, and none at
    all in who printed it, and a type with no field for a person cannot leak one
    however this is later edited. The same reasoning as
    `RefillerKioskResponse` having no price fields.

    No filenames either: a document title is often the most identifying thing
    about a job.
    """

    id: str
    state: str
    payment_method: str | None
    total_inr: Decimal
    sheets: int
    document_count: int
    paid_at: datetime | None
    refunded_at: datetime | None
    created_at: datetime
