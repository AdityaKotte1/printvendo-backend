"""How a Raspberry Pi proves it is the machine at a particular kiosk.

Three properties, each closing something the backend being replaced left open:

* **A device token is bound to one kiosk.** The old `/pi/*` routes checked that
  a token was valid and then trusted the printer id in the URL, so one shop's Pi
  could fetch another shop's job file. Here the token *is* the kiosk: nothing
  the device sends says which kiosk it is.
* **The long-lived credential is never handled by a person.** An operator
  generates a one-time enrolment code, SSHes into the Pi, and the agent spends
  it. What comes back is the token. A code that has been typed into a terminal
  is worthless a moment later, and the token exists only in the agent's config
  and as a hash in the database.
* **Re-enrolling replaces.** A kiosk has at most one device, so swapping a Pi
  kills the old one's access in the same breath -- that machine may have been
  sold, returned or stolen.

Whether a device is *online* is derived from its last heartbeat rather than read
from a status column. A Pi whose power was pulled cannot send "I am going
offline", and the old backend's status therefore stayed ONLINE until a person
noticed.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Unauthorized
from app.modules.kiosks.enums import DeviceStatus, OnboardingStage
from app.modules.kiosks.models import DeviceEnrolment, Kiosk, KioskDevice

# Long enough to walk to the shop and finish an install, short enough that a
# code left in a terminal's scrollback is not a way in next week.
ENROLMENT_LIFETIME = timedelta(hours=12)

# How recently a device must have reported to count as online. The agent
# heartbeats far more often than this; the window only has to be longer than one
# missed beat so a slow network does not make a working kiosk vanish.
HEARTBEAT_WINDOW = timedelta(minutes=5)

TOKEN_PREFIX = "dvt_"
CODE_PREFIX = "dve_"

# One sentence for unknown, expired and already-spent. Distinguishing them tells
# whoever is guessing which of their guesses was nearly right.
INVALID_CODE = "That enrolment code is invalid, has expired, or has already been used."
NOT_A_DEVICE = "This device is not registered. Re-enrol it from the dashboard."


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class IssuedEnrolment:
    """A one-time code and when it stops working.

    Both, because every caller has to tell the installer how long they have --
    and an expiry recomputed at the call site is an expiry that will eventually
    disagree with the row.
    """

    code: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedDevice:
    """A registration result. `token` is shown once and never again."""

    device: KioskDevice
    token: str


def device_of(db: Session, kiosk: Kiosk) -> KioskDevice | None:
    return db.execute(
        select(KioskDevice).where(KioskDevice.kiosk_id == kiosk.id)
    ).scalar_one_or_none()


# ── enrolment ───────────────────────────────────────────────────────────────


def issue_enrolment_code(
    db: Session, kiosk: Kiosk, *, created_by_user_id: int | None
) -> IssuedEnrolment:
    """Mint a one-time code for this kiosk, superseding any earlier open one.

    Two live codes for one kiosk means two machines could enrol, and only one of
    them is the one being installed.
    """
    now = datetime.now(UTC)

    db.query(DeviceEnrolment).filter(
        DeviceEnrolment.kiosk_id == kiosk.id,
        DeviceEnrolment.used_at.is_(None),
    ).update({"used_at": now})

    code = f"{CODE_PREFIX}{secrets.token_urlsafe(24)}"
    expires_at = now + ENROLMENT_LIFETIME
    db.add(
        DeviceEnrolment(
            kiosk_id=kiosk.id,
            created_by_user_id=created_by_user_id,
            code_hash=_hash(code),
            expires_at=expires_at,
        )
    )
    return IssuedEnrolment(code=code, expires_at=expires_at)


def register_device(
    db: Session,
    code: str,
    *,
    device_key: str | None = None,
    agent_version: str | None = None,
    capabilities: str | None = None,
) -> IssuedDevice:
    """Spend an enrolment code and hand back a device token.

    `device_key` is what the agent already had, if anything. It is an identifier
    rather than a credential -- it is what an operator reads back over SSH to
    tell one physical box from another, so it survives re-registration.
    """
    row = db.execute(
        select(DeviceEnrolment).where(DeviceEnrolment.code_hash == _hash(code or ""))
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise BadRequest(INVALID_CODE)

    row.used_at = now
    db.add(row)

    kiosk = db.get(Kiosk, row.kiosk_id)
    if kiosk is None or kiosk.onboarding_stage is OnboardingStage.RETIRED:
        raise BadRequest(INVALID_CODE)

    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"

    existing = device_of(db, kiosk)
    if existing is not None:
        # Replace in place rather than adding a second row. The unique
        # constraint on kiosk_id would refuse the insert anyway; doing it
        # explicitly is what makes the old token dead the moment a new Pi
        # enrols, which is the point.
        existing.token_hash = _hash(token)
        existing.device_key = device_key or existing.device_key
        existing.agent_version = agent_version or existing.agent_version
        existing.capabilities = capabilities or existing.capabilities
        existing.status = DeviceStatus.OFFLINE
        existing.last_heartbeat_at = None
        db.add(existing)
        db.flush()
        return IssuedDevice(device=existing, token=token)

    device = KioskDevice(
        kiosk_id=kiosk.id,
        device_key=device_key or secrets.token_hex(16),
        token_hash=_hash(token),
        agent_version=agent_version,
        capabilities=capabilities,
        status=DeviceStatus.OFFLINE,
    )
    db.add(device)
    db.flush()
    return IssuedDevice(device=device, token=token)


def rotate_token(db: Session, device: KioskDevice) -> str:
    """Issue a fresh token and kill the current one."""
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    device.token_hash = _hash(token)
    db.add(device)
    return token


def revoke_device(db: Session, kiosk: Kiosk) -> None:
    """Detach the machine from this kiosk. Its token stops working at once."""
    db.query(KioskDevice).filter(KioskDevice.kiosk_id == kiosk.id).delete()


# ── authentication ──────────────────────────────────────────────────────────


def authenticate_device(db: Session, token: str | None) -> KioskDevice:
    """Turn a device token into the device it belongs to, or refuse.

    The kiosk is a property of the token. Nothing a device sends may name a
    different one, which is what stops one shop's Pi reading another's work.
    """
    if not token or not token.strip():
        raise Unauthorized(NOT_A_DEVICE)

    device = db.execute(
        select(KioskDevice).where(KioskDevice.token_hash == _hash(token))
    ).scalar_one_or_none()
    if device is None:
        raise Unauthorized(NOT_A_DEVICE)

    kiosk = db.get(Kiosk, device.kiosk_id)
    if kiosk is None or kiosk.onboarding_stage is OnboardingStage.RETIRED:
        raise Unauthorized(NOT_A_DEVICE)

    return device


# ── liveness ────────────────────────────────────────────────────────────────


def record_heartbeat(
    db: Session,
    device: KioskDevice,
    *,
    agent_version: str | None = None,
    status: DeviceStatus = DeviceStatus.ONLINE,
    now: datetime | None = None,
) -> KioskDevice:
    device.last_heartbeat_at = now or datetime.now(UTC)
    device.status = status
    if agent_version:
        device.agent_version = agent_version
    db.add(device)
    return device


def is_online(device: KioskDevice | None, *, now: datetime | None = None) -> bool:
    """Whether this device has reported recently enough to be believed.

    Derived, never stored: `status` is the last thing the device *claimed*, and
    a machine that lost power never gets to claim anything again.
    """
    if device is None or device.last_heartbeat_at is None:
        return False
    now = now or datetime.now(UTC)
    return (now - device.last_heartbeat_at) <= HEARTBEAT_WINDOW
