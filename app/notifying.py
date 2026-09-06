"""Which notifier the application uses, decided once.

A use case rather than a module, for the same reason `provisioning` and
`refunding` are: it spans core's notifier seam and the ops context, which no
bounded context may do, and it sits below the composition roots so both of them
run one implementation.

Both roots need it. `app/api` builds one per request through `get_notifier`, and
`app/jobs` needs one to tell an owner their shop has gone quiet -- and the two
roots may not import each other (`app.jobs | app.api` in `.importlinter`). Left
in `deps.py` this would have had to be copied into the sweep, and then "is Brevo
configured" would have had two answers that agreed until one of them changed.
"""

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.notifier import BrevoNotifier, LoggingNotifier, Notifier
from app.modules.ops import AlertSeverity, raise_alert


def notifier_for(settings: Settings, db: Session) -> Notifier:
    """How out-of-band messages leave the system.

    Brevo when a key is configured, the logging one otherwise -- so a developer
    with no key still completes a verification flow by reading the log, and
    production does not quietly do the same thing while believing it sends mail.

    A failed send raises an admin alert. `BrevoNotifier` cannot do that itself:
    core may not import a bounded context. This can, and an invitation that
    never arrived is exactly the kind of silent failure the alerts table exists
    for -- a shop waits for an email nobody knows was lost.
    """
    if not settings.BREVO_API_KEY.strip():
        return LoggingNotifier()

    def report(kind: str, email: str) -> None:
        # Deduplicated on the kind alone rather than on the address: when the
        # provider is down every send fails, and one alert saying "email is not
        # going out" is what an operator needs. A thousand rows naming a
        # thousand recipients is the wall of identical notifications that made
        # the old backend's console unreadable.
        raise_alert(
            db,
            kind="email.send.failed",
            severity=AlertSeverity.CRITICAL,
            summary=(
                "Email is not being delivered. Invitations and password "
                "resets are not arriving."
            ),
            dedupe_key="email.send.failed",
            detail={"last_failure": kind},
        )

    return BrevoNotifier(
        api_key=settings.BREVO_API_KEY,
        app_base_url=settings.APP_BASE_URL,
        sender_email=settings.MAIL_FROM_EMAIL,
        sender_name=settings.MAIL_FROM_NAME,
        on_failure=report,
    )
