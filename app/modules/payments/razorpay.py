"""The one place this system talks to Razorpay over the wire.

Three HTTP calls, written out rather than taken from Razorpay's SDK. The whole
payment flow is expressed against the `RazorpayGateway` protocol, so the routing
rules and the signature check are exercised without a network; pulling in the
SDK would add its own transport, its own exception hierarchy and its own retry
opinions for the sake of two endpoints, and every one of those would then need
translating back into this module's errors anyway.

**Failure is never silent.** Every path out of here either returns an id that
Razorpay actually sent or raises. A client that returned `""` on a malformed
response would have `open_checkout` store an empty `razorpay_order_id` -- which
is unique-indexed, so the *second* such payment would die on a constraint
violation, a long way from the request that caused it.

**The key secret never reaches an error message.** `RazorpayError` is a
`Conflict`, and `detail` on a Conflict is rendered straight to the student. Only
Razorpay's own description is carried through; the credentials are not in scope
at the point the message is built, which is the mechanism rather than a habit.
"""

import logging

import httpx

from app.core.errors import Conflict
from app.modules.payments.charges import Credentials

logger = logging.getLogger(__name__)

BASE_URL = "https://api.razorpay.com/v1"

# Razorpay's own header. Our idempotency key goes in it, so "already done" means
# the same thing on their side as on ours.
IDEMPOTENCY_HEADER = "X-Razorpay-Idempotency-Key"

GATEWAY_UNAVAILABLE = "We could not reach the payment provider. Please try again."
GATEWAY_REFUSED = "The payment provider refused that request."


class RazorpayError(Conflict):
    """Razorpay refused, or could not be reached.

    A `Conflict` rather than a bare exception so it arrives at the api layer as
    a sentence a student can read, instead of a 500 and a stack trace. The
    caller cannot proceed either way -- there is no order id and no refund id --
    so it is a refusal, not an internal error.
    """


class HttpRazorpay:
    """The `RazorpayGateway` a real student's payment goes through.

    `transport` exists so tests can drive the real httpx client against a stub
    at the transport layer: URL building, basic auth encoding and JSON
    serialisation are then the library's actual behaviour rather than a mock's
    idea of it, and nobody needs live keys to run the suite.
    """

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    # ── the protocol ────────────────────────────────────────────────────────

    def create_order(
        self, *, amount_paise: int, receipt: str, credentials: Credentials
    ) -> str:
        """Open an order and return its razorpay_order_id."""
        body = self._post(
            "/orders",
            json={"amount": amount_paise, "currency": "INR", "receipt": receipt},
            credentials=credentials,
        )
        return self._id_from(body, what="order")

    def refund(
        self,
        *,
        razorpay_payment_id: str,
        amount_paise: int,
        idempotency_key: str,
        credentials: Credentials,
    ) -> str:
        """Return money to its source and return Razorpay's refund id.

        The amount is always sent, even for a full refund. Omitting it asks
        Razorpay to refund the whole payment, which is a different instruction
        from "refund this much" and would quietly disagree with the amount we
        are about to write down.
        """
        body = self._post(
            f"/payments/{razorpay_payment_id}/refund",
            json={"amount": amount_paise},
            credentials=credentials,
            headers={IDEMPOTENCY_HEADER: idempotency_key},
        )
        return self._id_from(body, what="refund")

    # ── the wire ────────────────────────────────────────────────────────────

    def _post(
        self,
        path: str,
        *,
        json: dict,
        credentials: Credentials,
        headers: dict[str, str] | None = None,
    ) -> dict:
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.post(
                    path,
                    json=json,
                    auth=(credentials.key_id, credentials.key_secret),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            # Timeouts, DNS, TLS, connection resets. Logged with the path but
            # never the body: an order body is harmless, a refund body is not
            # worth the habit.
            logger.error("razorpay: %s %s failed: %s", "POST", path, exc)
            raise RazorpayError(GATEWAY_UNAVAILABLE) from exc

        if response.status_code >= 400:
            raise RazorpayError(self._describe(response))

        try:
            body = response.json()
        except ValueError as exc:
            logger.error("razorpay: %s returned a non-JSON body", path)
            raise RazorpayError(GATEWAY_REFUSED) from exc

        if not isinstance(body, dict):
            logger.error("razorpay: %s returned %s, not an object", path, type(body))
            raise RazorpayError(GATEWAY_REFUSED)

        return body

    @staticmethod
    def _describe(response: httpx.Response) -> str:
        """Razorpay's own words for what was wrong, or ours if it did not say.

        Their descriptions are written for the person who made the request
        ("amount too low", "payment already fully refunded"), which is more use
        to a student than a status code. Nothing about the credentials is in
        scope here, so nothing about them can leak into the sentence.
        """
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                description = error.get("description")
                if isinstance(description, str) and description.strip():
                    return description.strip()

        return f"{GATEWAY_REFUSED} (status {response.status_code})"

    @staticmethod
    def _id_from(body: dict, *, what: str) -> str:
        """The id Razorpay assigned, or a refusal.

        A 200 carrying something other than what we asked for is a failure, not
        an empty result. See this module's docstring for what an empty string
        would do three steps later.
        """
        value = body.get("id")
        if not isinstance(value, str) or not value:
            logger.error("razorpay: %s response carried no id", what)
            raise RazorpayError(GATEWAY_REFUSED)
        return value
