"""What the rate limiter must do, and which routes must be covered by it.

The coverage test is the mechanism: it reads the authz matrix rather than a
second hand-kept list, so a new PUBLIC route fails the build until somebody
decides whether it may be hammered.
"""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.api.ratelimit import LIMITS, UNLIMITED, client_key
from app.core.config import Settings
from app.main import create_app
from tests.authz.matrix import MATRIX, PUBLIC, WEBSOCKET
from tests.authz.test_matrix_complete import declared_routes

SECRET = "s" * 32


def _settings(**extra) -> Settings:
    return Settings(
        ENV="dev",
        DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
        REDIS_URL="redis://localhost:6379/0",
        JWT_SECRET_KEY=SECRET,
        SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        CORS_ORIGINS="http://localhost:3000",
        **extra,
    )


class SilentNotifier:
    def send_email_verification(self, *, email: str, token: str) -> None:
        pass

    def send_password_reset(self, *, email: str, token: str) -> None:
        pass


@pytest.fixture
def make_client(db_session):
    """A client per settings, each with its own limiter storage."""

    def _make(**extra) -> TestClient:
        app = create_app(_settings(**extra))
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_secret] = lambda: SECRET
        app.dependency_overrides[get_notifier] = lambda: SilentNotifier()
        return TestClient(app, raise_server_exceptions=False)

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client(TRUST_PROXY_HEADERS=True)


# ── coverage ────────────────────────────────────────────────────────────────


def test_every_public_route_is_limited_or_deliberately_not():
    """Anything reachable without a credential is the attack surface.

    Derived from the authz matrix so there is no second list to keep in step.
    A route may be left unlimited, but only by saying so and why.
    """
    public = {
        (method, path)
        for (method, path), audiences in MATRIX.items()
        if audiences == {PUBLIC} and method != WEBSOCKET
    }
    undecided = public - set(LIMITS) - set(UNLIMITED)
    assert undecided == set(), (
        "These routes can be called without signing in and nobody has decided "
        f"whether they may be hammered: {sorted(undecided)}"
    )


def test_no_rule_names_a_route_that_does_not_exist():
    """Collected by the authz harness rather than by a third route collector.

    Two of them already disagreed about what a WebSocket route looks like; a
    third would be a third thing to teach every time a route kind is added.
    """
    real = set(declared_routes())
    dead = {key for key in (set(LIMITS) | set(UNLIMITED)) if key not in real}
    assert dead == set(), f"Rules for routes that no longer exist: {sorted(dead)}"


def test_every_rule_is_a_literal_path():
    """The middleware matches on the raw path, before routing has happened.

    A templated rule would therefore never fire, and would look like coverage.
    Adding one means teaching the middleware to resolve templates first.
    """
    templated = [path for _, path in LIMITS if "{" in path]
    assert templated == []


def test_every_unlimited_entry_gives_a_reason():
    assert all(reason.strip() for reason in UNLIMITED.values())


# ── the key ─────────────────────────────────────────────────────────────────


def test_the_key_is_the_socket_peer_when_no_proxy_is_trusted():
    key = client_key(
        headers={"x-forwarded-for": "9.9.9.9"}, peer="10.0.0.1", trust_proxy=False
    )
    assert key == "10.0.0.1"


def test_a_trusted_proxy_contributes_the_last_forwarded_hop():
    """Our proxy appends the peer that connected to it.

    Anything to the left of that was supplied by the caller, so taking the
    leftmost entry would let one client mint a fresh bucket per request by
    prepending a made-up address.
    """
    key = client_key(
        headers={"x-forwarded-for": "1.2.3.4, 203.0.113.7"},
        peer="10.0.0.1",
        trust_proxy=True,
    )
    assert key == "203.0.113.7"


def test_a_forged_forwarded_header_cannot_split_the_bucket():
    first = client_key(
        headers={"x-forwarded-for": "203.0.113.7"}, peer="10.0.0.1", trust_proxy=True
    )
    forged = client_key(
        headers={"x-forwarded-for": "8.8.8.8, 203.0.113.7"},
        peer="10.0.0.1",
        trust_proxy=True,
    )
    assert first == forged


# ── behaviour ───────────────────────────────────────────────────────────────

FORGOT = "/v1/app/auth/forgot-password"
FORGOT_PER_MINUTE = 10


def _forgot(client: TestClient, ip: str = "203.0.113.7"):
    return client.post(
        FORGOT,
        json={"email": "someone@example.com"},
        headers={"x-forwarded-for": ip},
    )


def test_a_caller_under_the_limit_is_served(client):
    for _ in range(FORGOT_PER_MINUTE):
        assert _forgot(client).status_code == 202


def test_the_caller_over_the_limit_gets_429_with_a_sentence(client):
    for _ in range(FORGOT_PER_MINUTE):
        _forgot(client)

    refused = _forgot(client)
    assert refused.status_code == 429
    assert refused.json()["detail"].endswith(".")
    assert refused.headers["retry-after"].isdigit()


def test_the_limit_is_per_caller(client):
    for _ in range(FORGOT_PER_MINUTE + 1):
        _forgot(client, ip="203.0.113.7")

    assert _forgot(client, ip="198.51.100.4").status_code == 202


def test_a_shared_socket_peer_shares_the_bucket_when_no_proxy_is_trusted(make_client):
    """Without TRUST_PROXY_HEADERS the header is not evidence of anything."""
    client = make_client(TRUST_PROXY_HEADERS=False)
    for _ in range(FORGOT_PER_MINUTE):
        _forgot(client, ip="203.0.113.7")

    assert _forgot(client, ip="198.51.100.4").status_code == 429


def test_an_unlimited_route_is_never_refused(client):
    for _ in range(FORGOT_PER_MINUTE * 3):
        assert client.get("/health").status_code == 200


def test_a_refused_request_never_reaches_the_route(client):
    """The point of limiting at the edge: no database work, no email sent."""
    app = client.app
    app.dependency_overrides[get_db] = _explodes

    for _ in range(FORGOT_PER_MINUTE):
        _forgot(client)
    assert _forgot(client).status_code == 429


def _explodes():
    raise AssertionError("the route ran despite being rate limited")


def test_a_refusal_still_carries_the_cors_headers(client):
    """A 429 the browser cannot read is reported to the user as a network error.

    That means the rate limiter must sit *inside* the CORS middleware, which is
    the opposite of the order add_middleware calls read in.
    """
    for _ in range(FORGOT_PER_MINUTE):
        _forgot(client)

    refused = client.post(
        FORGOT,
        json={"email": "someone@example.com"},
        headers={"x-forwarded-for": "203.0.113.7", "origin": "http://localhost:3000"},
    )
    assert refused.status_code == 429
    assert refused.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_limiting_can_be_turned_off_entirely(make_client):
    """One switch, for a load test or an incident. Never the default."""
    client = make_client(TRUST_PROXY_HEADERS=True, RATE_LIMIT_ENABLED=False)
    for _ in range(FORGOT_PER_MINUTE * 2):
        assert _forgot(client).status_code == 202
