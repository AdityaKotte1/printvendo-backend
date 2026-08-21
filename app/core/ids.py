"""Opaque, prefixed public identifiers.

The old backend exposed both a numeric primary key and a public string for the
same printer, and callers passed the wrong one. Here the database key is never
serialised: every id crossing the API is `<prefix>_<16 chars>`, and parse_id
refuses an id of the wrong kind, so a document id can never be accepted where a
kiosk id belongs.

The alphabet is Crockford base32 minus i/l/o/u, so ids survive being read aloud.
"""

import secrets
from enum import StrEnum

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
BODY_LENGTH = 16


class IdPrefix(StrEnum):
    USER = "usr"
    KIOSK = "ksk"
    DEVICE = "dev"
    DOCUMENT = "doc"
    ORDER = "ord"
    PRINT_TASK = "tsk"
    PAYMENT = "pay"
    REFUND = "rfd"
    WALLET_ENTRY = "wlt"
    SUBSCRIPTION = "sub"
    ALERT = "alr"
    PAYMENT_CONFIG_CHANGE = "pcr"


class InvalidId(ValueError):
    """Raised when an id is malformed or of the wrong kind."""


def new_id(prefix: IdPrefix) -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LENGTH))
    return f"{prefix.value}_{body}"


def parse_id(value: str, expected: IdPrefix) -> str:
    """Return the body of `value`, or raise InvalidId."""
    if not isinstance(value, str) or "_" not in value:
        raise InvalidId(f"Malformed identifier: {value!r}")

    prefix, _, body = value.partition("_")
    if prefix != expected.value:
        raise InvalidId(f"Expected a {expected.value}_ identifier, got {prefix!r}")
    if len(body) != BODY_LENGTH or not set(body) <= set(ALPHABET):
        raise InvalidId(f"Malformed identifier body: {value!r}")
    return body
