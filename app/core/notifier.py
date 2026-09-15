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

import html
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

    def send_refund_to_wallet(
        self, *, email: str, amount_inr: str, balance_inr: str, order_id: str
    ) -> None:
        """Tell `email` that a refund is in their balance, and what it is now."""
        ...

    def send_refund_to_source(
        self, *, email: str, amount_inr: str, order_id: str
    ) -> None:
        """Tell `email` that a refund is on its way back to how they paid."""
        ...

    def send_kiosk_offline(
        self, *, email: str, kiosk_name: str, last_seen: str | None
    ) -> None:
        """Tell `email` that `kiosk_name` stopped answering."""
        ...

    def send_paper_low(
        self, *, email: str, kiosk_name: str, sheets_remaining: int
    ) -> None:
        """Tell `email` that `kiosk_name`'s tray is nearly or completely empty."""
        ...


class LoggingNotifier:
    """Writes what would have been sent. The default until a provider lands."""

    def send_email_verification(self, *, email: str, token: str) -> None:
        logger.info("email verification for %s -- token %s", email, token)

    def send_password_reset(self, *, email: str, token: str) -> None:
        logger.info("password reset for %s -- token %s", email, token)

    def send_staff_invite(self, *, email: str, token: str, kiosk_name: str) -> None:
        logger.info("staff invite for %s to %s -- token %s", email, kiosk_name, token)

    def send_refund_to_wallet(
        self, *, email: str, amount_inr: str, balance_inr: str, order_id: str
    ) -> None:
        logger.info(
            "refund to balance for %s -- %s on %s, balance now %s",
            email, amount_inr, order_id, balance_inr,
        )

    def send_refund_to_source(
        self, *, email: str, amount_inr: str, order_id: str
    ) -> None:
        logger.info("refund to source for %s -- %s on %s", email, amount_inr, order_id)

    def send_kiosk_offline(
        self, *, email: str, kiosk_name: str, last_seen: str | None
    ) -> None:
        logger.info("kiosk offline to %s -- %s, last seen %s", email, kiosk_name, last_seen)

    def send_paper_low(
        self, *, email: str, kiosk_name: str, sheets_remaining: int
    ) -> None:
        logger.info(
            "paper low to %s -- %s, %s sheets left", email, kiosk_name, sheets_remaining
        )


class NullNotifier:
    """Sends nothing at all. For tests that do not care."""

    def send_email_verification(self, *, email: str, token: str) -> None:
        return None

    def send_password_reset(self, *, email: str, token: str) -> None:
        return None

    def send_staff_invite(self, *, email: str, token: str, kiosk_name: str) -> None:
        return None

    def send_refund_to_wallet(
        self, *, email: str, amount_inr: str, balance_inr: str, order_id: str
    ) -> None:
        return None

    def send_refund_to_source(
        self, *, email: str, amount_inr: str, order_id: str
    ) -> None:
        return None

    def send_kiosk_offline(
        self, *, email: str, kiosk_name: str, last_seen: str | None
    ) -> None:
        return None

    def send_paper_low(
        self, *, email: str, kiosk_name: str, sheets_remaining: int
    ) -> None:
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

    **Anything a person chose is escaped before it reaches the body.** Only one
    such value exists today -- a kiosk name -- and it arrives here having been
    typed by an owner into a form, then sent to an address of their choosing.
    That is a way to put arbitrary markup in front of a stranger under our
    sending domain, so it is escaped at the point of interpolation. Adding a
    second such value means escaping that one too; there is no template engine
    here doing it by default.
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

        # A kiosk name is text an owner chose, and an invitation goes to
        # whatever address they typed. Interpolated raw it is a way to send
        # arbitrary styled content -- a link somewhere else, wearing our sending
        # domain -- to a stranger with every reason to trust it. Escaped, a shop
        # genuinely called "Ram & Sons" still reads correctly.
        safe_name = html.escape(kiosk_name)

        self._send(
            kind="staff_invite",
            email=email,
            # The shop is named in the subject on purpose: "you have been
            # invited" with no shop name reads as phishing to the person who
            # receives it, and an invitation nobody trusts is not an invitation.
            # Not escaped here: a subject is plain text, and `&amp;` in it would
            # be shown literally. It travels as JSON, so there is no header to
            # inject into.
            subject=f"You have been invited to {kiosk_name} on Printvendo",
            body=(
                f"<p>You have been invited to work at <b>{safe_name}</b>.</p>"
                f'<p><a href="{link}">Accept the invitation</a></p>'
                "<p>Nothing is shared with them until you accept.</p>"
            ),
        )

    def send_refund_to_wallet(
        self, *, email: str, amount_inr: str, balance_inr: str, order_id: str
    ) -> None:
        """Money back into a Printvendo balance.

        The balance is stated because it is the question the message otherwise
        provokes -- somebody told "₹20 has gone back" then opens the app to
        check, and if the two disagree they write to us. Both numbers come from
        the same ledger read, so they cannot.
        """
        self._send(
            kind="refund_to_wallet",
            email=email,
            subject=f"₹{amount_inr} is back in your Printvendo balance",
            body=(
                f"<p>We have put <b>₹{amount_inr}</b> back into your Printvendo "
                "balance.</p>"
                f"<p>Your balance is now <b>₹{balance_inr}</b>.</p>"
                f"<p>This was for order {order_id}.</p>"
                "<p>You can spend it on your next print. Nothing else is "
                "needed from you.</p>"
            ),
        )

    def send_refund_to_source(
        self, *, email: str, amount_inr: str, order_id: str
    ) -> None:
        """Money back the way it came.

        Deliberately says "we have sent it" rather than "you have it": the
        refund leaves Razorpay immediately and arrives when the card network or
        the bank decides. Promising it instantly is how somebody comes to write
        in on day one of a two-day wait.
        """
        self._send(
            kind="refund_to_source",
            email=email,
            subject=f"Your refund of ₹{amount_inr} is on its way",
            body=(
                f"<p>We have refunded <b>₹{amount_inr}</b> for order "
                f"{order_id}.</p>"
                "<p>It goes back to whatever you paid with, and will show on "
                "your bank or card statement.</p>"
                "<p>Some banks show it straight away. If it is not there yet, "
                "it usually arrives within <b>1 to 2 working days</b>.</p>"
                "<p>You do not need to do anything.</p>"
            ),
        )

    def send_kiosk_offline(
        self, *, email: str, kiosk_name: str, last_seen: str | None
    ) -> None:
        """A shop stopped answering.

        The name is escaped for the same reason a staff invite's is: an owner
        typed it, and this message goes to people who have every reason to
        trust our sending domain.
        """
        safe_name = html.escape(kiosk_name)
        when = (
            f"<p>It was last heard from at <b>{html.escape(last_seen)}</b>.</p>"
            if last_seen
            else "<p>It has not been heard from at all.</p>"
        )
        self._send(
            kind="kiosk_offline",
            email=email,
            # Named in the subject: an operator with several shops needs to
            # know which one from the notification, not after opening it.
            subject=f"{kiosk_name} is offline",
            body=(
                f"<p><b>{safe_name}</b> has stopped answering, so students are "
                "not being offered it and nothing can print there.</p>"
                + when
                + "<p>Usual causes: the machine is switched off, the shop's "
                "internet is down, or the agent has stopped.</p>"
                "<p>You will not get another email about this shop until it "
                "comes back and goes offline again.</p>"
            ),
        )

    def send_paper_low(
        self, *, email: str, kiosk_name: str, sheets_remaining: int
    ) -> None:
        """A tray is nearly or completely empty.

        Written for whoever can fix it -- usually a refiller -- so it carries a
        sheet count and nothing about money, which a refiller's own surface does
        not carry either. The name is escaped for the reason the offline email's
        is: an owner typed it.
        """
        safe_name = html.escape(kiosk_name)
        if sheets_remaining <= 0:
            subject = f"{kiosk_name} is out of paper"
            state = (
                f"<p><b>{safe_name}</b> is out of paper. Students cannot print "
                "there until it is refilled.</p>"
            )
        else:
            subject = f"{kiosk_name} has {sheets_remaining} sheets left"
            state = (
                f"<p><b>{safe_name}</b> has <b>{sheets_remaining}</b> sheets of "
                "paper left, and stops taking orders when it runs out.</p>"
            )
        self._send(
            kind="paper_low",
            email=email,
            # Named in the subject, as the offline one is: somebody refilling
            # several shops needs to know which from the notification.
            subject=subject,
            body=(
                state
                + "<p>Please refill the tray and record the refill in the "
                "Printvendo app, so the count matches what is in the printer.</p>"
                "<p>You will not get another email about this tray until it has "
                "been refilled and runs low again.</p>"
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
