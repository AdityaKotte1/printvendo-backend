"""The socket a kiosk's Pi holds open.

What it is for: a job created on any worker reaches the machine in the shop
within a moment, instead of at the next poll. What it is deliberately *not*:
a second way to claim work. The socket carries a wake -- "there is something
queued" -- and the device then claims over the existing HTTP path, which is one
`FOR UPDATE SKIP LOCKED` statement. Pushing the task itself would mean two
implementations of claiming, and a reconnect overlapping a publish could hand
the same job to two devices.

The properties under test:

* the token **is** the kiosk, exactly as on every other device route, so a
  socket cannot ask about another shop;
* an unauthenticated socket is closed rather than served;
* a wake for another kiosk never arrives here.
"""

import pytest
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis, FakeServer, FakeStrictRedis
from fastapi.testclient import TestClient

from app.api.deps import get_bus, get_db
from app.core.bus import RedisBus
from app.core.config import Settings
from app.main import create_app
from app.modules.kiosks.devices import issue_enrolment_code, register_device
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk

SECRET = "s" * 32
BOX_KEY = Fernet.generate_key().decode()
SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=BOX_KEY,
    CORS_ORIGINS="https://printvendo.com",
    PUBLIC_BASE_URL="https://api.printvendo.com",
)

SOCKET = "/v1/device/ws"


@pytest.fixture
def bus() -> RedisBus:
    server = FakeServer()
    return RedisBus(
        sync=FakeStrictRedis(server=server),
        async_factory=lambda: FakeAsyncRedis(server=server),
    )


@pytest.fixture
def client(db_session, bus) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_bus] = lambda: bus
    return TestClient(app)


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(
        name="Socket Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def device_headers(db_session, kiosk) -> dict[str, str]:
    """A real enrolment, through the path a Pi actually takes. Hand-writing a
    token hash here would test this file's idea of the scheme rather than the
    scheme."""
    code = issue_enrolment_code(db_session, kiosk, created_by_user_id=None).code
    issued = register_device(db_session, code)
    db_session.flush()
    return {"X-Device-Token": issued.token}


def test_a_device_with_a_valid_token_is_greeted(client, device_headers):
    """The greeting is what tells an agent its socket is live rather than merely
    open -- a TCP connection that was never authenticated looks identical from
    the client's side until something is sent."""
    with client.websocket_connect(SOCKET, headers=device_headers) as socket:
        assert socket.receive_json() == {"type": "ready"}


def test_a_wake_for_this_kiosk_arrives(client, kiosk, bus, device_headers):
    with client.websocket_connect(SOCKET, headers=device_headers) as socket:
        socket.receive_json()

        bus.wake(kiosk.id)

        assert socket.receive_json() == {"type": "wake"}


def test_a_wake_carries_no_work(client, kiosk, bus, device_headers):
    """It is a hint, not a delivery. If the payload travelled here there would
    be two implementations of claiming, and a reconnect overlapping a publish
    could hand one job to two devices."""
    with client.websocket_connect(SOCKET, headers=device_headers) as socket:
        socket.receive_json()
        bus.wake(kiosk.id)

        message = socket.receive_json()

    assert set(message) == {"type"}


def test_the_socket_listens_only_on_its_own_kiosks_channel(
    client, kiosk, bus, db_session, device_headers
):
    """One shop's Pi must never be woken by another shop's job.

    This half of the rule is the route's: it subscribes for the kiosk its
    *token* names, and never for one named in anything it receives. The other
    half -- that a channel identifies exactly one kiosk -- belongs to the bus and
    is tested there.

    Asserted on what the socket subscribes to rather than on which message turns
    up first. An earlier version published another kiosk's wake and expected
    this kiosk's; it could not fail, because both messages read
    `{"type": "wake"}` and the wrong one is indistinguishable from the right
    one. Making `channel()` ignore its argument left that version green.
    """
    other = Kiosk(
        name="Other Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(other)
    db_session.flush()

    subscribed: list[int] = []
    real_wakeups = bus.wakeups

    def recording(kiosk_id: int):
        subscribed.append(kiosk_id)
        return real_wakeups(kiosk_id)

    bus.wakeups = recording  # type: ignore[method-assign]

    with client.websocket_connect(SOCKET, headers=device_headers) as socket:
        socket.receive_json()

    assert subscribed == [kiosk.id]
    assert other.id not in subscribed


def test_a_socket_without_a_token_is_closed(client, kiosk):
    """The same rule as every other device route: the token is the kiosk, and
    there is no kiosk id in the URL for a handler to trust."""
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(SOCKET) as socket:
            socket.receive_json()


def test_a_socket_with_a_wrong_token_is_closed(client, kiosk):
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            SOCKET, headers={"X-Device-Token": "not-the-token"}
        ) as socket:
            socket.receive_json()
