"""Google sign-in.

The id token is verified locally with google-auth against Google's cached
public keys. An earlier approach in the backend being replaced put the raw
token in a URL, which leaks it into every log along the way.

The verifier is injected so tests can exercise this without the network. The
default is the real one.
"""

import secrets
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.security import hash_password
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

Verifier = Callable[[str, str], dict]

COULD_NOT_VERIFY = "That Google sign-in could not be verified."


def verify_with_google(token: str, client_id: str) -> dict:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        token,
        google_requests.Request(),
        client_id,
        clock_skew_in_seconds=10,
    )


def sign_in_with_google(
    db: Session,
    token: str,
    client_id: str,
    verifier: Verifier = verify_with_google,
) -> User:
    if not client_id:
        raise BadRequest("Google sign-in is not available right now.")

    try:
        claims = verifier(token, client_id)
    except ValueError as exc:
        raise BadRequest(COULD_NOT_VERIFY) from exc
    except Exception as exc:  # noqa: BLE001 - google-auth raises broadly
        raise BadRequest(COULD_NOT_VERIFY) from exc

    email = claims.get("email")
    if not email:
        raise BadRequest("That Google account has no email address.")

    # Google sets email_verified=False for some Workspace configurations. An
    # unverified address here would let someone claim an account for a mailbox
    # they do not control, so it is refused rather than trusted.
    if claims.get("email_verified") is False:
        raise BadRequest("That Google account's email address is not verified.")

    user = repo.get_by_email(db, email)
    if user is not None:
        # Signing in with Google proves the address, even for an account that
        # was originally registered with a password and never confirmed.
        user.email_verified = True
        db.add(user)
        return user

    # Password is random and never told to anyone: this account signs in with
    # Google. It is set rather than left null so the column stays NOT NULL and
    # every code path can assume a hash is present.
    user = User(
        email=email,
        full_name=claims.get("name"),
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        email_verified=True,
    )
    db.add(user)
    db.flush()
    repo.grant_role(db, user.id, Role.STUDENT)
    return user
