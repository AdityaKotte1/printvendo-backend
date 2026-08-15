"""How the application tells a person something out-of-band.

A seam, not an implementation. Identity issues a verification token but must
not know whether it travels by Brevo, SMTP or SMS -- wiring a provider into the
auth module would make every test that registers a user depend on it, and would
put template concerns inside a bounded context that has no business holding
them.

The real adapter arrives with the ops work. Until then LoggingNotifier records
the call, so a developer can complete a verification flow locally by reading
the log, and nothing silently pretends to have sent an email.
"""

import logging
from typing import Protocol

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
