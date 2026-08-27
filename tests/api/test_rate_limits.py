"""What the rate limiter must do, and which routes must be covered by it.

The coverage test is the mechanism: it reads the authz matrix rather than a
second hand-kept list, so a new PUBLIC route fails the build until somebody
decides whether it may be hammered.
"""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.api.ratelimit import LIMITS, UNLIMITED, client_key
from app.core.config import Settings
from app.core.security import TokenType, create_token
from app.main import create_app
from tests.authz.matrix import MATRIX, PUBLIC, WEBSOCKET
from tests.authz.test_matrix_complete import declared_routes

SECRET = "s" * 32


def _settings(**extra) -> Settings:
    extra.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/pv")
    return Settings(
        ENV="dev",
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
def make_client(db_session, postgres_url):
    """A client per settings, each with its own limiter storage.

    Pointed at the real test database because `/health` runs `select 1` now, and
    the unlimited-route test below would otherwise be reading a 503.
    """

    def _make(**extra) -> TestClient:
        app = create_app(_settings(DATABASE_URL=postgres_url, **extra))
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
        # 200 rather than "not 429": a probe that started failing for some other
        # reason would otherwise satisfy this test for ever.
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


# ── the bucket a person gets, rather than the one a building shares ─────────
#
# Every limit above is per address, and a campus is one address: two hundred
# students behind one NAT share a bucket, so a single script degrades the
# lecture hall around it and the ceilings have to be set loose enough that the
# hall still works. That is the trade this section removes.
#
# A request carrying a *verified* token is keyed on the account instead. It has
# to be verified: keying on an unchecked `sub` would let anybody mint a fresh
# bucket per request by editing a claim, which is worse than no per-account
# limit at all. An unverifiable token falls back to the address, which is also
# what the request itself is about to be refused for.


def _token(subject: str = "usr_aaaaaaaaaaaaaaaa") -> str:
    return create_token(subject, TokenType.ACCESS, SECRET, timedelta(minutes=5))


def _bearer(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


def test_two_accounts_behind_one_address_do_not_share_a_bucket(client):
    """The whole point. One student's script must not spend the budget of
    everybody else on the campus wifi."""
    route = "/v1/app/auth/change-password"
    body = {"current_password": "x" * 12, "new_password": "y" * 12}
    mine_headers = _bearer("usr_aaaaaaaaaaaaaaaa")

    assert ("POST", route) in LIMITS

    for _ in range(25):
        client.post(route, json=body, headers=mine_headers)

    # The first account is spent. The second, from the same address, is not.
    mine = client.post(route, json=body, headers=mine_headers)
    theirs = client.post(route, json=body, headers=_bearer("usr_bbbbbbbbbbbbbbbb"))

    assert mine.status_code == 429
    assert theirs.status_code != 429


def test_a_wrongly_signed_subject_cannot_mint_a_fresh_bucket(client):
    """The check that makes the account bucket a limit rather than a courtesy.

    These tokens are **well formed** and carry a different `sub` each time --
    what they do not carry is our signature. A limiter that decoded the claim
    without verifying it would hand out a fresh bucket per request for the price
    of editing a field, which is worse than having no per-account limit at all.

    Garbage like `Bearer not.a.token` does not test this: an unverified decoder
    rejects that too, so the assertion would pass either way. It has to be a
    token that a decoder *would* believe.
    """
    body = {"current_password": "x" * 12, "new_password": "y" * 12}

    seen = set()
    for n in range(80):
        forged = create_token(
            f"usr_{n:016d}", TokenType.ACCESS, "n" * 32, timedelta(minutes=5)
        )
        seen.add(
            client.post(
                "/v1/app/auth/change-password",
                json=body,
                headers={"Authorization": f"Bearer {forged}"},
            ).status_code
        )
        if 429 in seen:
            break

    assert 429 in seen, "a forged subject was handed its own bucket every time"


def test_an_anonymous_caller_is_still_limited_by_address(client):
    """Nothing about adding an account bucket may loosen the one that catches a
    caller with no credential at all."""
    seen = set()
    for _ in range(40):
        seen.add(client.post("/v1/app/auth/login", json={}).status_code)

    assert 429 in seen


def test_one_address_cannot_mint_accounts_without_bound(client):
    """The backstop. Per-account buckets alone would mean one machine could do
    as much as it liked by rotating a claim, so the address ceiling stays --
    just far enough above a single account's to leave a busy campus alone.
    """
    body = {"current_password": "x" * 12, "new_password": "y" * 12}

    seen = set()
    for n in range(600):
        seen.add(
            client.post(
                "/v1/app/auth/change-password",
                json=body,
                headers=_bearer(f"usr_{n:016d}"),
            ).status_code
        )
        if 429 in seen:
            break

    assert 429 in seen, "an address rotating accounts was never refused"
