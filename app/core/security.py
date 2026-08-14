"""Password hashing and JWT issue/verify.

Every token carries an explicit `typ` claim and decode_token requires the
expected one, so a long-lived refresh token can never be replayed where a
15-minute access token is required. Every token also carries a unique `jti`,
which is what makes refresh rotation with reuse detection possible in the
identity module.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from jose import JWTError, jwt
from passlib.context import CryptContext

ALGORITHM = "HS256"

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """Token missing, malformed, expired, mistyped or badly signed."""


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    token_type: TokenType
    jti: str
    family: str | None


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd.verify(password, hashed)
    except ValueError:
        return False


def create_token(
    subject: str,
    token_type: TokenType,
    secret: str,
    lifetime: timedelta,
    family: str | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": subject,
        "typ": token_type.value,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    if family is not None:
        payload["fam"] = family
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, expected: TokenType, secret: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise TokenError(str(exc)) from exc

    if payload.get("typ") != expected.value:
        raise TokenError(f"Expected a {expected.value} token")

    return TokenClaims(
        subject=payload["sub"],
        token_type=expected,
        jti=payload["jti"],
        family=payload.get("fam"),
    )
