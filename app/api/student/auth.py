"""Sign-in for the student app.

The refresh token goes in an httpOnly cookie and never appears in a response
body -- a token in JSON is readable by any script on the page. The access token
does go in the body, because the client must attach it to each request; that is
why it lives fifteen minutes.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    get_db,
    get_notifier,
    get_secret,
    get_settings_from_app,
)
from app.core.config import Settings
from app.core.errors import Unauthorized
from app.core.notifier import Notifier
from app.modules.identity import repository as repo
from app.modules.identity.google import sign_in_with_google
from app.modules.identity.guests import create_guest
from app.modules.identity.models import User
from app.modules.identity.passwords import authenticate, register
from app.modules.identity.sessions import (
    REFRESH_LIFETIME,
    issue_tokens,
    revoke_refresh,
    rotate_refresh,
)
from app.modules.identity.verification import start_verification, verify_email

router = APIRouter(prefix="/v1/app/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/v1/app/auth"
NO_SESSION = "You need to sign in to do that."


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleRequest(BaseModel):
    id_token: str


class VerifyEmailRequest(BaseModel):
    token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_guest: bool = False
    email_verified: bool = False


class MeResponse(BaseModel):
    id: str
    email: str
    full_name: str | None
    is_guest: bool
    email_verified: bool
    roles: list[str]


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        # Secure cookies are not sent over plain http, which is what local
        # development uses. Everywhere else it is on.
        secure=settings.ENV != "dev",
        samesite="lax",
        max_age=int(REFRESH_LIFETIME.total_seconds()),
        path=COOKIE_PATH,
    )


def _signed_in(
    user: User, db: Session, response: Response, secret: str, settings: Settings
) -> TokenResponse:
    access, refresh = issue_tokens(db, user, secret)
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(
        access_token=access,
        is_guest=user.is_guest,
        email_verified=user.email_verified,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register_account(
    payload: RegisterRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> TokenResponse:
    user = register(db, payload.email, payload.password, payload.full_name)

    token = start_verification(db, user)
    notifier.send_email_verification(email=user.email, token=token)

    return _signed_in(user, db, response, secret, settings)


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> TokenResponse:
    user = authenticate(db, payload.email, payload.password)
    return _signed_in(user, db, response, secret, settings)


@router.post("/guest", response_model=TokenResponse)
def guest_login(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> TokenResponse:
    user = create_guest(db)
    return _signed_in(user, db, response, secret, settings)


@router.post("/google", response_model=TokenResponse)
def google_login(
    payload: GoogleRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> TokenResponse:
    user = sign_in_with_google(db, payload.id_token, settings.GOOGLE_CLIENT_ID)
    return _signed_in(user, db, response, secret, settings)


@router.post("/refresh", response_model=TokenResponse)
def refresh_session(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenResponse:
    if not refresh_token:
        raise Unauthorized(NO_SESSION)

    access, new_refresh = rotate_refresh(db, refresh_token, secret)
    _set_refresh_cookie(response, new_refresh, settings)
    return TokenResponse(access_token=access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    db: Annotated[Session, Depends(get_db)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> Response:
    if refresh_token:
        revoke_refresh(db, refresh_token)

    # Build the response we actually return and clear the cookie on *that*.
    # Taking an injected `response` and then returning a different Response
    # object silently discards the cookie deletion, leaving the browser holding
    # a revoked token and the user apparently still signed in.
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    return response


@router.post("/verify-email", response_model=MeResponse)
def confirm_email(
    payload: VerifyEmailRequest,
    db: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    user = verify_email(db, payload.token)
    return _me(user, db)


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
def resend_verification(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> dict[str, str]:
    # Answer the same way whether or not anything was sent. A different
    # response for an already-verified account would confirm the state of an
    # address to anyone holding a token for it.
    if not user.email_verified and not user.is_guest:
        token = start_verification(db, user)
        notifier.send_email_verification(email=user.email, token=token)

    return {"detail": "If that address needs verifying, a new link is on its way."}


def _me(user: User, db: Session) -> MeResponse:
    return MeResponse(
        id=user.public_id,
        email=user.email,
        full_name=user.full_name,
        is_guest=user.is_guest,
        email_verified=user.email_verified,
        roles=sorted(r.value for r in repo.roles_of(db, user.id)),
    )


@router.get("/me", response_model=MeResponse)
def me(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    return _me(user, db)
