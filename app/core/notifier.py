"""How the application tells a person something out-of-band.

A seam, not an implementation. Identity issues a verification token but must
not know whether it travels by Brevo, SMTP or SMS -- wiring a provider into the
auth module would make every test that registers a user depend on it, and would
put template concerns inside a bounded context that has no business holding
them.

`BrevoNotifier` is the real adapter. `LoggingNotifier` remains the default when
no key is configured, so a developer can complete a verification flow locally by
reading the log and nothing silently pretends to have sent an email.
"""

import logging
from collections.abc import Callable
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send_email_verification(self, *, email: str, token: str) -> None:
        """Deliver a verification link for `token` to `email`."""
        ...

    def send_password_reset(self, *, email: str, token: str) -> None:
        """Deliver a password-reset link for `token` to `email`."""
        ...

    def send_staff_invite(self, *, email: str, token: str, kiosk_name: str) -> None:
        """Invite `email` to work at `kiosk_name`, using `token`."""
        ...


class LoggingNotifier:
    """Writes what would have been sent. The default until a provider lands."""

    def send_email_verification(self, *, email: str, token: str) -> None:
        logger.info("email verification for %s -- token %s", email, token)

    def send_password_reset(self, *, email: str, token: str) -> None:
        logger.info("password reset for %s -- token %s", email, token)

    def send_staff_invite(self, *, email: str, token: str, kiosk_name: str) -> None:
        logger.info("staff invite for %s to %s -- token %s", email, kiosk_name, token)


class NullNotifier:
    """Sends nothing at all. For tests that do not care."""

    def send_email_verification(self, *, email: str, token: str) -> None:
        return None

    def send_password_reset(self, *, email: str, token: str) -> None:
        return None

    def send_staff_invite(self, *, email: str, token: str, kiosk_name: str) -> None:
        return None


BREVO_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_TIMEOUT_SECONDS = 10.0


class BrevoNotifier:
    """The real adapter. One HTTP call per message, written out.

    **Nothing here raises.** A provider outage must not turn a successful
    registration into a 500, and must not turn "if that address exists, a link
    is on its way" into a stack trace that proves the address does exist -- the
    enumeration oracle the forgot-password wording exists to avoid.

    That leaves the failure invisible, which is precisely how this whole seam
    became inert, so a failure is handed to `on_failure`. Core may not import a
    bounded context, so this cannot raise an admin alert itself; the composition
    root wires one in.

    `transport` exists so tests drive the real httpx client against a stub: URL
    building, header encoding and JSON serialisation are then the library's
    actual behaviour rather than a mock's idea of it, and nobody needs a live
    key to run the suite.
    """

    def __init__(
        self,
        *,
        api_key: str,
        app_base_url: str,
        sender_email: str,
        sender_name: str,
        transport: object | None = None,
        on_failure: Callable[[str, str], None] | None = None,
    ) -> None:
        if not api_key.strip():
            # A notifier with no key would accept every send and deliver
            # nothing, which is the failure this class exists to end wearing a
            # new costume. get_notifier picks LoggingNotifier when unset.
            raise ValueError("BrevoNotifier needs an API key.")

        self._api_key = api_key
        self._app = app_base_url.rstrip("/")
        self._sender = {"email": sender_email, "name": sender_name}
        self._transport = transport
        self._on_failure = on_failure

    # ── the three messages ──────────────────────────────────────────────────

    def send_email_verification(self, *, email: str, token: str) -> None:
        link = f"{self._app}/verify-email?token={token}"
        self._send(
            kind="email_verification",
            email=email,
            subject="Confirm your email address",
            body=(
                "<p>Welcome to Printvendo.</p>"
                f'<p><a href="{link}">Confirm your email address</a></p>'
                "<p>If you did not create an account, you can ignore this.</p>"
            ),
        )

    def send_password_reset(self, *, email: str, token: str) -> None:
        link = f"{self._app}/reset-password?token={token}"
        self._send(
            kind="password_reset",
            email=email,
            subject="Reset your Printvendo password",
            body=(
                "<p>Someone asked to reset the password on this account.</p>"
                f'<p><a href="{link}">Choose a new password</a></p>'
                "<p>If that was not you, nothing has changed and you can ignore "
                "this.</p>"
            ),
        )

    def send_staff_invite(self, *, email: str, token: str, kiosk_name: str) -> None:
        link = f"{self._app}/accept-invite?token={token}"
        self._send(
            kind="staff_invite",
            email=email,
            # The shop is named in the subject on purpose: "you have been
            # invited" with no shop name reads as phishing to the person who
            # receives it, and an invitation nobody trusts is not an invitation.
            subject=f"You have been invited to {kiosk_name} on Printvendo",
            body=(
                f"<p>You have been invited to work at <b>{kiosk_name}</b>.</p>"
                f'<p><a href="{link}">Accept the invitation</a></p>'
                "<p>Nothing is shared with them until you accept.</p>"
            ),
        )

    # ── the wire ────────────────────────────────────────────────────────────

    def _send(self, *, kind: str, email: str, subject: str, body: str) -> None:
        payload = {
            "sender": self._sender,
            "to": [{"email": email}],
            "subject": subject,
            "htmlContent": body,
        }

        try:
            with httpx.Client(
                timeout=BREVO_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = client.post(
                    BREVO_URL,
                    json=payload,
                    headers={"api-key": self._api_key, "accept": "application/json"},
                )
            if response.status_code >= 400:
                self._failed(kind, email, f"provider refused: {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            self._failed(kind, email, f"could not be reached: {type(exc).__name__}")

    def _failed(self, kind: str, email: str, why: str) -> None:
        """Report a send that did not happen, without leaking the token.

        The token is a credential, and a production log is somewhere people
        paste into tickets. The address is enough to say which message failed.
        """
        logger.error("could not send %s to %s: %s", kind, email, why)

        if self._on_failure is None:
            return
        try:
            self._on_failure(kind, email)
        except Exception:  # noqa: BLE001
            # The alert is a courtesy on an already-failed path. Somebody's
            # registration is not the alert's business.
            logger.warning("could not report the failed %s either", kind, exc_info=True)
