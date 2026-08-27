"""The matrix, actually exercised.

`test_matrix_complete` proves every route is *declared*. It does not prove any
declaration is *true* — a route could say `{ADMIN}` and be wired with no role
dependency at all, and nothing would notice. `matrix.py` has said since it was
written that "later plans add, alongside this table, the test that actually
exercises each route as each audience". This is that test.

For every route, every audience the matrix does **not** name is sent at it, and
the answer must be a refusal. Refusal means 401 (who are you), 403 (not you) or
404 (nothing here for you) — the last is deliberate, because a kiosk outside
somebody's scope is 404 rather than 403 so that one shop owner learns nothing
true about a competitor's estate.

**422 counts as a failure.** It means the request got past authorisation as far
as body validation: the handler was reachable, and only the shape of the request
stopped it. That is precisely the drift this file exists to catch.

Path parameters are filled with well-formed ids that match nothing. A route that
refuses on scope answers 404 for them, which is a refusal; a route that does not
check at all answers 200 or 422, which is not.
"""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.core.config import Settings
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from tests.authz.matrix import (
    ADMIN,
    DEVICE,
    MATRIX,
    OWNER,
    PUBLIC,
    REFILLER,
    STUDENT,
    WEBSOCKET,
)

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="http://localhost:3000",
    # Off, or the sixth call to a limited route in this sweep is refused for
    # the wrong reason and the test would report a hole it has not found.
    RATE_LIMIT_ENABLED=False,
)

REFUSALS = {401, 403, 404}

# Well-formed and matching nothing. `parse_id` accepts the shape, the lookup
# finds no row, and a route that checks its scope answers 404.
NOWHERE = {
    "kiosk_id": "ksk_aaaaaaaaaaaaaaaa",
    "order_id": "ord_aaaaaaaaaaaaaaaa",
    "document_id": "doc_aaaaaaaaaaaaaaaa",
    "task_id": "tsk_aaaaaaaaaaaaaaaa",
    "account_id": "usr_aaaaaaaaaaaaaaaa",
    "owner_id": "usr_aaaaaaaaaaaaaaaa",
    "user_id": "usr_aaaaaaaaaaaaaaaa",
    "alert_id": "alr_aaaaaaaaaaaaaaaa",
    "request_id": "pcr_aaaaaaaaaaaaaaaa",
    "role": "owner",
    "plan_id": "1",
    "duration_months": "3",
}

# Audiences that carry a bearer token. DEVICE authenticates with a header of its
# own, so "as a device" is covered by the no-token case below rather than by a
# token this test could mint.
BEARER_AUDIENCES = (STUDENT, OWNER, REFILLER, ADMIN)

ROLE_OF = {
    STUDENT: None,
    OWNER: Role.OWNER,
    REFILLER: Role.REFILLER,
    ADMIN: Role.ADMIN,
}


def _fill(path: str) -> str:
    for name, value in NOWHERE.items():
        path = path.replace("{" + name + "}", value)
    return path


@pytest.fixture(scope="module")
def routes() -> list[tuple[str, str, set[str]]]:
    return [
        (method, path, audiences)
        for (method, path), audiences in sorted(MATRIX.items())
        if method != WEBSOCKET
    ]


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def tokens(db_session) -> dict[str, str]:
    """One account per audience, holding exactly that audience's role."""
    issued: dict[str, str] = {}
    for audience in BEARER_AUDIENCES:
        user = User(email=f"{audience}@authz.example", hashed_password="x")
        db_session.add(user)
        db_session.flush()
        identity_repo.grant_role(db_session, user.id, Role.STUDENT)
        role = ROLE_OF[audience]
        if role is not None:
            identity_repo.grant_role(db_session, user.id, role)
        db_session.flush()
        issued[audience] = create_token(
            user.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5)
        )
    return issued


def _call(client: TestClient, method: str, path: str, headers: dict) -> int:
    return client.request(method, _fill(path), headers=headers, json={}).status_code


def test_the_sweep_covers_every_route(routes):
    """A sweep that quietly collected nothing would prove nothing."""
    assert len(routes) > 80


def test_a_route_that_is_not_public_refuses_an_anonymous_caller(client, routes):
    unguarded = []
    for method, path, audiences in routes:
        if PUBLIC in audiences:
            continue
        status = _call(client, method, path, {})
        if status not in REFUSALS:
            unguarded.append((method, path, status))

    assert unguarded == [], (
        "These routes answered an anonymous caller with something other than a "
        f"refusal: {unguarded}"
    )


def test_every_route_refuses_every_audience_it_does_not_name(client, tokens, routes):
    """The claim in the matrix, tested rather than asserted.

    A 422 fails here on purpose: it means the caller reached body validation, so
    whatever stopped them was the shape of their request rather than who they
    are.
    """
    leaks = []
    for method, path, audiences in routes:
        if PUBLIC in audiences:
            # Anybody may call it, so there is no audience to refuse.
            continue

        for audience in BEARER_AUDIENCES:
            if audience in audiences:
                continue
            # An admin is a wider scope rather than a separate router, so a
            # route naming DEVICE only is still refused to them by the device
            # credential check.
            status = _call(
                client, method, path, {"Authorization": f"Bearer {tokens[audience]}"}
            )
            if status not in REFUSALS:
                leaks.append((method, path, f"as {audience}", status))

    assert leaks == [], (
        "These routes answered an audience the matrix does not name: "
        f"{leaks}"
    )


def test_device_routes_refuse_a_persons_token(client, tokens, routes):
    """A device credential is not a session, and the two must not be
    interchangeable: a student holding a bearer token must not be able to claim
    print work."""
    reachable = []
    for method, path, audiences in routes:
        if audiences != {DEVICE}:
            continue
        status = _call(
            client, method, path, {"Authorization": f"Bearer {tokens[STUDENT]}"}
        )
        if status not in REFUSALS:
            reachable.append((method, path, status))

    assert reachable == []


# ── the other direction ─────────────────────────────────────────────────────
#
# Everything above proves the matrix's *refusals*. Nothing proved its
# *permissions*, and that asymmetry has already cost: `/v1/owner/earnings*`
# allowed OWNER|ADMIN on the router and then narrowed to `require_role(OWNER)`
# on every route, so four routes 403'd an audience the matrix promised — for
# however long it had been there, with a green build the whole time.
#
# A named audience must not be turned away by *who they are*. What it may get
# is 404 (well-formed id, nothing behind it), 422 (an empty body reached
# validation, so authorisation let it through), 409, or a success. What it must
# never get is 401 or 403.

TURNED_AWAY = {401, 403}


def test_every_route_admits_every_audience_it_names(client, tokens, routes):
    """The matrix's promises, tested rather than asserted.

    404 is fine and 422 is fine -- both mean the caller reached the handler and
    was stopped by the request rather than by their identity. Only 401 and 403
    say "not you", and for a named audience that is the matrix lying.
    """
    refused = []
    for method, path, audiences in routes:
        for audience in BEARER_AUDIENCES:
            if audience not in audiences:
                continue
            status = _call(
                client, method, path, {"Authorization": f"Bearer {tokens[audience]}"}
            )
            if status in TURNED_AWAY:
                refused.append((method, path, f"as {audience}", status))

    assert refused == [], (
        "The matrix names these audiences, and the routes turned them away: "
        f"{refused}"
    )


def test_no_public_route_hides_behind_a_role(client, routes):
    """PUBLIC routes, and the narrower claim that is actually true of them.

    Not "answers 200 without a credential". Several of these have a credential
    of their own and are right to refuse without it: the Razorpay webhooks are
    PUBLIC because Razorpay holds no token of ours and **the signature is the
    authentication**, and `/refresh` and `/logout` authenticate with the refresh
    cookie. An unsigned webhook answering 401 is the mechanism working.

    What PUBLIC does rule out is a **role** guard. 403 means somebody was turned
    away for who they are, and a route with no audience to check cannot have an
    opinion about that -- so 403 here is always a declaration that has drifted.

    `/health` is skipped: it is a liveness probe for a load balancer rather than
    a route with an audience, and it answers by talking to a database this
    module deliberately does not have.
    """
    misdeclared = []
    for method, path, audiences in routes:
        if PUBLIC not in audiences or path == "/health":
            continue
        if _call(client, method, path, {}) == 403:
            misdeclared.append((method, path))

    assert misdeclared == [], (
        "These routes are declared PUBLIC and answered 403, which only a role "
        f"guard does: {misdeclared}"
    )


def test_the_admitting_sweep_actually_tried_something(client, tokens, routes):
    """The guard the negative sweep already has, for the same reason: a loop
    whose `continue` fired every time would pass while proving nothing."""
    tried = sum(
        1
        for _, _, audiences in routes
        for audience in BEARER_AUDIENCES
        if audience in audiences
    )

    assert tried > 150
