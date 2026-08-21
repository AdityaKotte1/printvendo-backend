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


# ── admin: reviewing where an owner's money goes ────────────────────────────


class ChangeRequestResponse(BaseModel):
    """One owner asking to replace the account their takings are paid into.

    `has_proof` rather than a path. There is deliberately no field a storage key
    could travel in: the old admin dashboard turned one into
    `API_BASE + '/storage/...'`, which 404s silently behind an `onerror`
    handler, so admins approved these having never seen the evidence. The bytes
    come from `/proof`, which authenticates.
    """

    id: str
    owner_id: str
    owner_email: str
    reason: str | None
    has_proof: bool
    status: str
    created_at: datetime
    reviewed_at: datetime | None
    review_note: str | None


class ReviewChangeRequest(BaseModel):
    approve: bool
    # Why. Recorded on the request and in the audit trail, and shown to nobody
    # automatically -- an owner told "rejected" with no reason will simply ask.
    note: str | None = None


# ── admin: what needs attention, and what was done ──────────────────────────


class AlertResponse(BaseModel):
    """One condition an operator should look at.

    `occurrences` is what makes the list readable: a kiosk offline for a week is
    one row seen 10,080 times, not 10,080 rows. The old backend's notifications
    table had no such notion, and a console showing a wall of identical rows is
    one nobody reads -- which looks like coverage and is not.
    """

    id: str
    kind: str
    severity: str
    summary: str
    detail: dict | None
    entity_type: str | None
    entity_id: str | None
    occurrences: int
    resolved: bool
    first_seen_at: datetime
    last_seen_at: datetime


class AuditEntryResponse(BaseModel):
    """One thing somebody did.

    The actor is a person -- public id and email -- never `actor_user_id`. Null
    for both when the system did it: "nobody did this" is a different fact from
    "we failed to record who", and flattening them loses the distinction the
    audit module went to the trouble of keeping.
    """

    action: str
    entity_type: str
    entity_id: str | None
    actor_id: str | None
    actor_email: str | None
    before: dict | None
    after: dict | None
    note: str | None
    created_at: datetime


# ── admin: a kiosk's life ───────────────────────────────────────────────────


class AdminKioskResponse(BaseModel):
    """A kiosk as the people who install and retire them see it.

    The one field the owner's view has no business carrying: **who owns it**.
    An admin decides which shop a kiosk belongs to, and an ownerless SOLD kiosk
    -- production has exactly one, quietly routing its takings to the platform
    -- is a thing this surface has to be able to show.
    """

    id: str
    name: str
    kiosk_type: str
    onboarding_stage: str
    onboarding_note: str | None
    is_active: bool
    is_selling: bool
    accepts_wallet: bool
    location_description: str | None
    owner_id: str | None
    owner_email: str | None
    paper: PaperResponse


class CreateKioskRequest(BaseModel):
    name: str
    # Defaults to the platform's own: a kiosk nobody has been told they own is
    # one whose money is ours to collect, which is the safe direction. Making it
    # SOLD by mistake would point takings at an account that does not exist.
    kiosk_type: str = "platform"
    location_description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    paper_capacity: int | None = None


class StageChangeRequest(BaseModel):
    stage: str
    # Why it moved, kept on the kiosk for whoever reads it next -- "waiting on
    # the shop's GST number" is the difference between a queue and a mystery.
    note: str | None = None


class KioskTypeChangeRequest(BaseModel):
    type: str


class InviteOwnerRequest(BaseModel):
    email: str


# ── admin: plans and what one owner pays ────────────────────────────────────


class PlanDiscountResponse(BaseModel):
    duration_months: int
    percent: Decimal


class PlanResponse(BaseModel):
    """A tier, its band, and its published discount ladder.

    The band comes back with the plan because it *is* a term of the plan -- an
    owner's kiosk prices are constrained by what they are paying for, and a
    second copy of the floor and ceiling in a client is a copy that drifts.
    """

    id: str
    name: str
    monthly_price: Decimal
    max_kiosks: int
    price_floor_bw: Decimal | None
    price_ceiling_bw: Decimal | None
    price_floor_color: Decimal | None
    price_ceiling_color: Decimal | None
    is_active: bool
    discounts: list[PlanDiscountResponse]


class CreatePlanRequest(BaseModel):
    name: str
    monthly_price: Decimal
    max_kiosks: int = 1
    price_floor_bw: Decimal | None = None
    price_ceiling_bw: Decimal | None = None
    price_floor_color: Decimal | None = None
    price_ceiling_color: Decimal | None = None


class UpdatePlanRequest(BaseModel):
    """Everything optional, and no `name`.

    A plan's name is what an owner sees on an invoice they already hold.
    Renaming it silently rewrites history, so it is not a field here.
    """

    monthly_price: Decimal | None = None
    max_kiosks: int | None = None
    price_floor_bw: Decimal | None = None
    price_ceiling_bw: Decimal | None = None
    price_floor_color: Decimal | None = None
    price_ceiling_color: Decimal | None = None
    is_active: bool | None = None


class SetDiscountRequest(BaseModel):
    duration_months: int
    percent: Decimal
    note: str | None = None


class OwnerDiscountResponse(BaseModel):
    duration_months: int
    percent: Decimal
    note: str | None


class SubscriptionResponse(BaseModel):
    """One owner's subscription as an admin needs to see it.

    `on_trial` is separate from `status` on purpose: a trialling owner is
    collecting real money into their own account while paying nothing, and a
    console that folds that into "active" cannot show the difference.
    """

    id: str
    plan_id: str
    plan_name: str
    status: str
    on_trial: bool
    in_force: bool
    monthly_price_charged: Decimal
    negotiated_price: Decimal | None
    total_amount: Decimal
    free_until: datetime | None
    starts_at: datetime | None
    expires_at: datetime | None


class OwnerBillingResponse(BaseModel):
    owner_id: str
    owner_email: str
    # Null when this owner has never had one -- not a fabricated free
    # subscription. "This shop cannot collect" is the fact worth showing.
    subscription: SubscriptionResponse | None
    discounts: list[OwnerDiscountResponse]


class GrantTrialRequest(BaseModel):
    plan_id: str
    days: int


class NegotiatePriceRequest(BaseModel):
    # Null puts this owner back on the plan's list price.
    monthly_price: Decimal | None = None


class QuoteResponse(BaseModel):
    """A price and every input that produced it.

    An invoice that cannot explain itself is how billing arguments start, so the
    rate, the discount and where the discount came from all travel with the
    total.
    """

    duration_months: int
    monthly_price: Decimal
    discount_percent: Decimal
    discount_source: str
    negotiated: bool
    total: Decimal


# ── admin: accounts and what they may do ────────────────────────────────────


class AccountResponse(BaseModel):
    """A person, as the console needs them.

    No password hash, no token, no session -- there is no field any of them
    could travel in. `roles` is the list from the role table rather than a set
    of booleans on the row: three loose flags is how the old backend expressed
    this, and it is why a refiller was one forgotten check away from money data.
    """

    id: str
    email: str
    full_name: str | None
    roles: list[str]
    is_active: bool
    is_guest: bool
    email_verified: bool
    created_at: datetime
