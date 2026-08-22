"""Actually sending the email.

`LoggingNotifier` has been the only implementation since identity landed, and in
production the app logger sits at WARNING -- so password reset, email
verification and every staff and owner invitation have been silently inert. An
invitation is how a kiosk gets its owner, so this is the difference between a
shop being sold and a shop being able to print.

Driven against a stub httpx transport rather than a mock client, so URL
building, header encoding and JSON serialisation are the library's real
behaviour. Nobody needs a live Brevo key to run the suite.

Two rules here are not preferences:

* **Sending never raises.** A provider outage must not turn a successful
  registration into a 500, and must not turn "if that address exists, a link is
  on its way" into a stack trace that reveals the address does exist.
* **A failure is reported somewhere a person looks.** Silently swallowed is how
  this became inert in the first place, so the adapter tells whoever wired it.
"""

import json

import httpx
import pytest

from app.core.notifier import BrevoNotifier

API_KEY = "brevo-key-value"
APP_URL = "https://printvendo.com"
TOKEN = "verification-token-value"


def _notifier(handler, *, on_failure=None) -> BrevoNotifier:
    return BrevoNotifier(
        api_key=API_KEY,
        app_base_url=APP_URL,
        sender_email="hello@printvendo.com",
        sender_name="Printvendo",
        transport=httpx.MockTransport(handler),
        on_failure=on_failure,
    )


def _accepting(seen: list) -> callable:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"messageId": "<abc@brevo>"})

    return handler


# -- what actually goes over the wire ---------------------------------------


def test_a_verification_email_reaches_brevo_with_the_key():
    seen: list[httpx.Request] = []

    _notifier(_accepting(seen)).send_email_verification(
        email="student@example.com", token=TOKEN
    )

    assert len(seen) == 1
    request = seen[0]
    assert request.url.host == "api.brevo.com"
    assert request.headers["api-key"] == API_KEY


def test_the_link_points_at_the_app_and_carries_the_token():
    """The link goes to the *app*, never to the API. A verification URL on
    `api.printvendo.com` lands a person on JSON."""
    seen: list[httpx.Request] = []

    _notifier(_accepting(seen)).send_email_verification(
        email="student@example.com", token=TOKEN
    )

    body = seen[0].content.decode()
    assert APP_URL in body
    assert TOKEN in body
    assert "api.printvendo.com" not in body


def test_the_recipient_is_the_person_being_written_to():
    seen: list[httpx.Request] = []

    _notifier(_accepting(seen)).send_password_reset(
        email="forgetful@example.com", token=TOKEN
    )

    assert "forgetful@example.com" in seen[0].content.decode()


def test_an_invitation_names_the_kiosk_it_is_for():
    """"You have been invited" with no shop name is a phishing email as far as
    the recipient can tell."""
    seen: list[httpx.Request] = []

    _notifier(_accepting(seen)).send_staff_invite(
        email="refiller@example.com", token=TOKEN, kiosk_name="Library Ground Floor"
    )

    assert "Library Ground Floor" in seen[0].content.decode()


def test_each_kind_of_message_is_distinguishable():
    """Three different subjects. One generic "Printvendo" email for a reset, a
    verification and an invitation would make the wrong one get ignored."""
    seen: list[httpx.Request] = []
    notifier = _notifier(_accepting(seen))

    notifier.send_email_verification(email="a@example.com", token=TOKEN)
    notifier.send_password_reset(email="a@example.com", token=TOKEN)
    notifier.send_staff_invite(email="a@example.com", token=TOKEN, kiosk_name="Shop")

    subjects = {json.loads(request.content)["subject"] for request in seen}
    assert len(subjects) == 3


# -- failing --------------------------------------------------------------


def test_a_refused_send_does_not_raise():
    """A provider outage must not turn a successful registration into a 500, and
    must not turn "if that address exists, a link is on its way" into a stack
    trace that proves the address exists."""

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "invalid sender"})

    _notifier(refusing).send_email_verification(email="a@example.com", token=TOKEN)


def test_an_unreachable_provider_does_not_raise():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _notifier(unreachable).send_password_reset(email="a@example.com", token=TOKEN)


def test_a_failure_is_reported_to_whoever_wired_it():
    """Silently swallowed is exactly how this became inert. The adapter cannot
    raise an admin alert itself -- core may not import a module -- so it hands
    the failure to the composition root, which can."""
    failures: list[tuple[str, str]] = []

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="brevo is unwell")

    notifier = _notifier(
        refusing, on_failure=lambda kind, email: failures.append((kind, email))
    )
    notifier.send_staff_invite(
        email="a@example.com", token=TOKEN, kiosk_name="Shop"
    )

    assert failures == [("staff_invite", "a@example.com")]


def test_a_failure_report_that_itself_fails_is_not_fatal():
    """The alert is a courtesy on an already-failed path. If raising it throws,
    the request must still succeed -- the user's registration is not the alert's
    business."""

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="brevo is unwell")

    def broken(kind: str, email: str) -> None:
        raise RuntimeError("the database is gone too")

    _notifier(refusing, on_failure=broken).send_email_verification(
        email="a@example.com", token=TOKEN
    )


def test_the_token_never_appears_in_a_log_line(caplog):
    """A reset token in a production log is a credential in a place people paste
    into tickets. The address is enough to tell somebody which send failed."""

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="brevo is unwell")

    with caplog.at_level("ERROR"):
        _notifier(refusing).send_password_reset(email="a@example.com", token=TOKEN)

    assert TOKEN not in caplog.text
    assert "a@example.com" in caplog.text


# -- not configured ---------------------------------------------------------


def test_without_an_api_key_it_refuses_to_be_built():
    """A Brevo notifier with no key would accept every send and deliver
    nothing -- the failure this whole file exists to end, wearing a new
    costume. `get_notifier` picks the logging one when no key is set."""
    with pytest.raises(ValueError):
        BrevoNotifier(
            api_key="",
            app_base_url=APP_URL,
            sender_email="hello@printvendo.com",
            sender_name="Printvendo",
        )


# -- a kiosk name is somebody else's text -----------------------------------


def test_markup_in_a_kiosk_name_cannot_reach_the_email_as_markup():
    """A kiosk name is chosen by an owner, and an invitation goes to whatever
    address they type. Interpolated raw, that is a way to send arbitrary styled
    content -- a link to somewhere else, wearing Printvendo's sending domain --
    to a stranger who has every reason to trust it.

    Escaped, the name still reads correctly to anyone whose shop genuinely
    contains an ampersand.
    """
    seen: list[httpx.Request] = []

    _notifier(_accepting(seen)).send_staff_invite(
        email="victim@example.com",
        token=TOKEN,
        kiosk_name='<a href="https://evil.example">Claim your prize</a>',
    )

    body = json.loads(seen[0].content)["htmlContent"]
    assert "<a href=\"https://evil.example\"" not in body
    assert "&lt;a href=" in body


def test_an_honest_name_with_an_ampersand_survives_readably():
    seen: list[httpx.Request] = []

    _notifier(_accepting(seen)).send_staff_invite(
        email="refiller@example.com", token=TOKEN, kiosk_name="Ram & Sons Xerox"
    )

    body = json.loads(seen[0].content)["htmlContent"]
    assert "Ram &amp; Sons Xerox" in body
