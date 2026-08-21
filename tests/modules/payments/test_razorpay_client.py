"""The real Razorpay client.

Everything else in the payment flow is tested against a protocol, which is what
lets the signature check and the routing rules be exercised without a network.
This file is the one place the actual HTTP is pinned, and it is driven with a
stub transport rather than a live account -- so the request that goes on the
wire is asserted (method, path, auth, body, idempotency header) without anyone
needing test keys to run the suite.

What is deliberately *not* mocked is httpx itself. The stub sits at the
transport layer, so URL building, basic auth encoding and JSON serialisation are
all the library's real behaviour.
"""

import base64
import json
from decimal import Decimal

import httpx
import pytest

from app.core.errors import Conflict
from app.core.money import to_paise
from app.modules.payments.charges import Credentials, RazorpayGateway
from app.modules.payments.razorpay import HttpRazorpay, RazorpayError

KEYS = Credentials("rzp_test_abc", "supersecret")


def client_with(handler) -> HttpRazorpay:
    """A client whose transport is a function, not a socket."""
    return HttpRazorpay(transport=httpx.MockTransport(handler), timeout=5.0)


def json_response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload)


# ── opening an order ────────────────────────────────────────────────────────


def test_creating_an_order_posts_the_amount_in_paise():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return json_response(200, {"id": "order_LiveOne", "status": "created"})

    order_id = client_with(handler).create_order(
        amount_paise=12345, receipt="ord_abc", credentials=KEYS
    )

    assert order_id == "order_LiveOne"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.razorpay.com/v1/orders"
    assert seen["body"] == {"amount": 12345, "currency": "INR", "receipt": "ord_abc"}
    # Basic auth, built by httpx from the credentials we were handed.
    assert seen["auth"].startswith("Basic ")


def test_the_order_is_opened_against_the_keys_it_was_given():
    """Two kiosks, two owners, two accounts. The credentials are per call
    because the gate decides them per kiosk."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        raw = base64.b64decode(request.headers["authorization"].split()[1]).decode()
        seen["user"] = raw.split(":")[0]
        return json_response(200, {"id": "order_X"})

    client_with(handler).create_order(
        amount_paise=100,
        receipt="r",
        credentials=Credentials("rzp_live_owner", "othersecret"),
    )

    assert seen["user"] == "rzp_live_owner"


# ── refunding ───────────────────────────────────────────────────────────────


def test_a_refund_posts_to_the_payment_and_carries_our_idempotency_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["idempotency"] = request.headers.get("x-razorpay-idempotency-key")
        return json_response(200, {"id": "rfnd_Live", "status": "processed"})

    refund_id = client_with(handler).refund(
        razorpay_payment_id="pay_REAL",
        amount_paise=2000,
        idempotency_key="our-key-1",
        credentials=KEYS,
    )

    assert refund_id == "rfnd_Live"
    assert seen["url"] == "https://api.razorpay.com/v1/payments/pay_REAL/refund"
    assert seen["body"] == {"amount": 2000}
    # Their header, our key. This is what makes a retry after a timeout the same
    # refund on their side as on ours rather than a second one.
    assert seen["idempotency"] == "our-key-1"


# ── failure is never silent ─────────────────────────────────────────────────


def test_a_rejected_request_raises_rather_than_returning_nothing():
    """Razorpay refusing must not read as success. A caller that got None here
    would write a Payment row for an order that was never opened."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            400,
            {"error": {"code": "BAD_REQUEST_ERROR", "description": "amount too low"}},
        )

    with pytest.raises(RazorpayError) as caught:
        client_with(handler).create_order(amount_paise=1, receipt="r", credentials=KEYS)

    assert "amount too low" in str(caught.value)


def test_a_response_with_no_id_is_an_error_not_an_empty_string():
    """A 200 whose body is not what we expect is still a failure. Returning ""
    would have us store an empty razorpay_order_id, which is unique -- so the
    *second* such payment would fail with a constraint violation instead, a long
    way from the cause."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"status": "created"})

    with pytest.raises(RazorpayError):
        client_with(handler).create_order(
            amount_paise=100, receipt="r", credentials=KEYS
        )


def test_a_network_failure_surfaces_as_a_gateway_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(RazorpayError):
        client_with(handler).create_order(
            amount_paise=100, receipt="r", credentials=KEYS
        )


def test_a_razorpay_error_is_a_conflict_so_the_api_answers_it_sensibly():
    """Errors are `{"detail": "<human sentence>"}` and the api layer renders
    `detail` straight to the user, so a gateway failure must already be one --
    not a stack trace, and not a 500."""
    assert issubclass(RazorpayError, Conflict)


def test_the_error_never_carries_the_key_secret():
    """A gateway error is logged and, as a Conflict, its message reaches the
    user. Nothing in it may include the credential that made the call."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(401, {"error": {"description": "authentication failed"}})

    with pytest.raises(RazorpayError) as caught:
        client_with(handler).create_order(
            amount_paise=100, receipt="r", credentials=KEYS
        )

    assert KEYS.key_secret not in str(caught.value)
    assert KEYS.key_secret not in repr(caught.value)


# ── it satisfies the protocol the module declares ───────────────────────────


def test_the_client_is_usable_as_the_gateway_the_module_asks_for():
    """A structural check, so the two cannot drift apart silently: the whole
    payment flow is written against `RazorpayGateway`, and this is the only
    implementation that will ever be in front of a real student."""

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"id": "order_ok"})

    gateway: RazorpayGateway = client_with(handler)
    assert gateway.create_order(amount_paise=100, receipt="r", credentials=KEYS)


def test_amounts_go_out_as_integers_never_as_rupees():
    """The one unit mistake that matters: sending 20 instead of 2000 charges a
    student twenty paise, and sending 2000 rupees charges them two thousand."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["amount"] = json.loads(request.content)["amount"]
        return json_response(200, {"id": "order_ok"})

    client_with(handler).create_order(
        amount_paise=to_paise(Decimal("20.00")), receipt="r", credentials=KEYS
    )

    assert seen["amount"] == 2000
    assert isinstance(seen["amount"], int)
