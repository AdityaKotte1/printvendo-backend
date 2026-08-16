"""Proving a Razorpay callback really came from Razorpay.

Everything downstream trusts these two functions: crediting a wallet, marking an
order paid, queueing a print. So they are written here rather than taken from
the SDK, for two reasons that matter more than the saved lines.

* **Failing closed is explicit.** An unconfigured secret returns False. The SDK
  raises, and a raised exception at the wrong call site becomes a 500 -- or,
  worse, a `try/except: pass` that accepts everything.
* **The comparison is `hmac.compare_digest`.** `==` on a hex digest
  short-circuits at the first differing byte, which leaks how much of a guess
  was right.

Both signatures are HMAC-SHA256, hex-encoded:

    payment callback   HMAC(razorpay_order_id + "|" + razorpay_payment_id)
    webhook            HMAC(the raw request body)

The webhook one is over **raw bytes**. Parsing the JSON and re-serialising it
before checking would let anyone change an amount and keep the signature.
"""

import hashlib
import hmac


def _matches(*, message: bytes, signature: str | None, secret: str) -> bool:
    """True only if `signature` is a genuine HMAC of `message` under `secret`.

    Every failure path returns False rather than raising. A malformed signature
    is an ordinary refusal; an exception would hand an attacker a way to tell
    a well-formed guess from a badly-formed one.
    """
    if not secret:
        # No key configured. Fail closed: a kiosk whose keys were cleared must
        # refuse every callback, not accept every forged one.
        return False
    if not signature or not signature.strip():
        return False

    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


SEPARATOR = "|"


def verify_payment_signature(
    *, order_id: str, payment_id: str, signature: str | None, secret: str
) -> bool:
    """Check the signature Razorpay hands the browser after a successful payment.

    Both ids are inside the signed string, so a valid signature from one payment
    cannot be replayed onto another -- which is what stops a student paying ₹2
    for one order and presenting that receipt for a ₹200 one.

    **An id containing the separator is refused outright.** Joining two fields
    with `|` is ambiguous: `("a|b", "c")` and `("a", "b|c")` sign the same
    string, so a signature issued for one pairing would verify for the other.
    Razorpay's own ids never contain a pipe, so this is not reachable through
    it -- but both ids arrive in a request body the client controls, and
    "unreachable because a third party's id format happens to exclude it" is a
    worse guarantee than one line of validation.
    """
    if SEPARATOR in order_id or SEPARATOR in payment_id:
        return False

    return _matches(
        message=f"{order_id}{SEPARATOR}{payment_id}".encode(),
        signature=signature,
        secret=secret,
    )


def verify_webhook_signature(*, body: bytes, signature: str | None, secret: str) -> bool:
    """Check the `X-Razorpay-Signature` header against the raw request body."""
    return _matches(message=body, signature=signature, secret=secret)
