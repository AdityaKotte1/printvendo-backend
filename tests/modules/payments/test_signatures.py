"""Proving a Razorpay callback really came from Razorpay.

Everything downstream -- crediting a wallet, marking an order paid, queueing a
print -- trusts these two functions. A signature check that can be made to
return True for an attacker-supplied body is the whole system.
"""

import hashlib
import hmac

import pytest

from app.modules.payments.signatures import (
    verify_payment_signature,
    verify_webhook_signature,
)

SECRET = "rzp_test_secret_value"


def sign(payload: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ── the payment callback ────────────────────────────────────────────────────


def test_a_genuine_payment_signature_is_accepted():
    signature = sign("order_ABC|pay_XYZ")

    assert (
        verify_payment_signature(
            order_id="order_ABC",
            payment_id="pay_XYZ",
            signature=signature,
            secret=SECRET,
        )
        is True
    )


def test_a_signature_for_a_different_payment_is_refused():
    """The order and payment ids are both inside the signed string, so a valid
    signature from one payment cannot be replayed onto another."""
    signature = sign("order_ABC|pay_XYZ")

    assert (
        verify_payment_signature(
            order_id="order_ABC",
            payment_id="pay_SOMEONE_ELSE",
            signature=signature,
            secret=SECRET,
        )
        is False
    )


def test_a_signature_for_a_different_order_is_refused():
    signature = sign("order_ABC|pay_XYZ")

    assert (
        verify_payment_signature(
            order_id="order_OTHER",
            payment_id="pay_XYZ",
            signature=signature,
            secret=SECRET,
        )
        is False
    )


def test_a_signature_made_with_another_key_is_refused():
    """This is what keeps one kiosk owner's Razorpay account from authorising
    payments at another's."""
    signature = sign("order_ABC|pay_XYZ", secret="somebody_elses_secret")

    assert (
        verify_payment_signature(
            order_id="order_ABC",
            payment_id="pay_XYZ",
            signature=signature,
            secret=SECRET,
        )
        is False
    )


@pytest.mark.parametrize("rubbish", ["", "   ", "not-hex", "0" * 64, None])
def test_rubbish_is_refused_rather_than_raising(rubbish):
    """A malformed signature is an ordinary refusal, not a 500. An exception
    here would be an attacker's way to tell valid-shaped from invalid-shaped."""
    assert (
        verify_payment_signature(
            order_id="order_ABC",
            payment_id="pay_XYZ",
            signature=rubbish,
            secret=SECRET,
        )
        is False
    )


def test_a_missing_secret_refuses_rather_than_accepting_everything():
    """An unconfigured key must fail closed. Failing open would make a kiosk
    whose keys were cleared accept every forged callback."""
    assert (
        verify_payment_signature(
            order_id="order_ABC",
            payment_id="pay_XYZ",
            signature=sign("order_ABC|pay_XYZ", secret=""),
            secret="",
        )
        is False
    )


def test_the_separator_cannot_be_smuggled_across_the_fields():
    """Concatenating with `|` means "a|b" and "a|b" must not be reachable from
    two different field splits -- otherwise an order id ending in a pipe could
    borrow another payment's signature."""
    signature = sign("order_A|B|pay_XYZ")

    # Both readings are refused, because an id carrying the separator is
    # refused outright -- the ambiguity is removed rather than reasoned about.
    assert not verify_payment_signature(
        order_id="order_A|B", payment_id="pay_XYZ", signature=signature, secret=SECRET
    )
    assert not verify_payment_signature(
        order_id="order_A",
        payment_id="B|pay_XYZ",
        signature=signature,
        secret=SECRET,
    )


# ── the webhook ─────────────────────────────────────────────────────────────


def test_a_genuine_webhook_body_is_accepted():
    body = b'{"event":"payment.captured"}'
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body=body, signature=signature, secret=SECRET)


def test_a_tampered_body_is_refused():
    """The signature is over the *raw bytes*. Re-serialising the JSON before
    checking would let an attacker change an amount and keep the signature."""
    body = b'{"event":"payment.captured","amount":100}'
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    tampered = b'{"event":"payment.captured","amount":100000}'

    assert not verify_webhook_signature(
        body=tampered, signature=signature, secret=SECRET
    )


def test_whitespace_changes_break_the_webhook_signature():
    """Proof that the check really is over raw bytes: JSON that parses
    identically but is spelled differently must not verify."""
    body = b'{"event":"payment.captured"}'
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    respaced = b'{ "event" : "payment.captured" }'

    assert not verify_webhook_signature(
        body=respaced, signature=signature, secret=SECRET
    )


@pytest.mark.parametrize("rubbish", ["", None, "not-hex", "ff" * 32])
def test_a_webhook_without_a_usable_signature_is_refused(rubbish):
    assert not verify_webhook_signature(
        body=b'{"event":"x"}', signature=rubbish, secret=SECRET
    )


def test_a_webhook_with_no_configured_secret_is_refused():
    assert not verify_webhook_signature(
        body=b'{"event":"x"}', signature="anything", secret=""
    )


def test_verification_is_constant_time():
    """Compared with hmac.compare_digest, not `==`.

    Asserted by reading the implementation rather than by timing, which is
    hopelessly flaky in CI -- but the property is worth pinning, because `==`
    short-circuits on the first differing byte and leaks the prefix.

    Reads the *comparing function's* source, not the module's. An earlier
    version inspected the whole module and was satisfied by the docstring
    above mentioning compare_digest -- so replacing the real comparison with
    `==` passed. A guardrail that can be satisfied by prose is not a guardrail.
    """
    import inspect

    from app.modules.payments.signatures import _matches

    body = inspect.getsource(_matches)
    comparison = body.split('"""')[-1]  # past the docstring

    assert "compare_digest" in comparison
    assert "==" not in comparison
