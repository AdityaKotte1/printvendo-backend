"""Alerts an operator will actually read.

The rule under test throughout: the same open problem is one row with a count.
The old backend's notifications table had no such notion, so the console filled
with identical rows and people stopped reading it — which is worse than having
no alerts, because it looks like coverage.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import NotFound
from app.modules.identity.models import User
from app.modules.ops import (
    AlertSeverity,
    open_alerts,
    raise_alert,
    resolve,
    resolve_by_key,
)


@pytest.fixture
def operator(db_session) -> User:
    user = User(email="ops@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


def offline(db_session, *, key="kiosk.offline:ksk_1", severity=AlertSeverity.WARNING, **kw):
    return raise_alert(
        db_session,
        kind="kiosk.offline",
        severity=severity,
        summary="Campus Print has not checked in for 20 minutes.",
        dedupe_key=key,
        entity_type="kiosk",
        entity_id="ksk_1",
        **kw,
    )


# ── one problem, one row ────────────────────────────────────────────────────


def test_the_same_open_problem_is_one_alert_with_a_count(db_session):
    """A kiosk offline for a week is one alert seen 10,080 times."""
    for _ in range(5):
        offline(db_session)

    alerts = open_alerts(db_session)
    assert len(alerts) == 1
    assert alerts[0].occurrences == 5


def test_a_recurrence_after_resolution_is_a_new_alert(db_session, operator):
    """The dedupe index is partial on `resolved = false` for this reason: "it
    came back" is exactly what an operator needs to see, and a plain unique
    index would hide it forever."""
    first = offline(db_session)
    resolve(db_session, public_id=first.public_id, actor_user_id=operator.id)

    second = offline(db_session)

    assert second.id != first.id
    assert second.occurrences == 1
    assert len(open_alerts(db_session)) == 1


def test_a_worsening_condition_reads_as_worse(db_session):
    offline(db_session, severity=AlertSeverity.WARNING)
    offline(db_session, severity=AlertSeverity.CRITICAL)

    assert open_alerts(db_session)[0].severity is AlertSeverity.CRITICAL


def test_an_alert_does_not_quietly_de_escalate(db_session):
    """Only a person closes an alert. A later, milder report of the same problem
    must not make it look handled."""
    offline(db_session, severity=AlertSeverity.CRITICAL)
    offline(db_session, severity=AlertSeverity.INFO)

    assert open_alerts(db_session)[0].severity is AlertSeverity.CRITICAL


def test_the_newest_detail_wins(db_session):
    offline(db_session, detail={"last_seen": "09:12"})
    offline(db_session, detail={"last_seen": "09:40"})

    assert open_alerts(db_session)[0].detail == {"last_seen": "09:40"}


def test_different_problems_stay_separate(db_session):
    offline(db_session, key="kiosk.offline:ksk_1")
    offline(db_session, key="kiosk.offline:ksk_2")

    assert len(open_alerts(db_session)) == 2


# ── the list is triageable ──────────────────────────────────────────────────


def test_the_list_is_ordered_by_severity_not_alphabetically(db_session):
    """The severity column is text. Ordering by it in SQL gives critical, info,
    warning -- which looks like a priority order and is not one."""
    now = datetime.now(UTC)
    raise_alert(
        db_session, kind="a", severity=AlertSeverity.INFO, summary="i",
        dedupe_key="i", now=now,
    )
    raise_alert(
        db_session, kind="b", severity=AlertSeverity.WARNING, summary="w",
        dedupe_key="w", now=now - timedelta(minutes=1),
    )
    raise_alert(
        db_session, kind="c", severity=AlertSeverity.CRITICAL, summary="c",
        dedupe_key="c", now=now - timedelta(minutes=2),
    )

    assert [a.severity for a in open_alerts(db_session)] == [
        AlertSeverity.CRITICAL,
        AlertSeverity.WARNING,
        AlertSeverity.INFO,
    ]


def test_a_resolved_alert_leaves_the_list(db_session, operator):
    alert = offline(db_session)

    resolve(db_session, public_id=alert.public_id, actor_user_id=operator.id)

    assert open_alerts(db_session) == []
    assert alert.resolved_by_user_id == operator.id
    assert alert.resolved_at is not None


def test_resolving_twice_is_not_an_error(db_session, operator):
    """Two operators clicking the same button is not a conflict worth a 409."""
    alert = offline(db_session)
    resolve(db_session, public_id=alert.public_id, actor_user_id=operator.id)

    again = resolve(db_session, public_id=alert.public_id, actor_user_id=operator.id)

    assert again.id == alert.id


def test_resolving_something_that_is_not_there_is_refused(db_session, operator):
    with pytest.raises(NotFound):
        resolve(db_session, public_id="alr_nothing", actor_user_id=operator.id)


def test_an_alert_has_its_own_kind_of_id(db_session):
    from app.core.ids import IdPrefix, parse_id

    alert = offline(db_session)

    assert alert.public_id.startswith("alr_")
    parse_id(alert.public_id, IdPrefix.ALERT)


def test_a_secret_in_the_detail_is_redacted(db_session):
    """Alert detail goes through the same scrubber as audit. An alert about a
    failing payment config must not carry the config."""
    alert = raise_alert(
        db_session,
        kind="payment.config.broken",
        severity=AlertSeverity.CRITICAL,
        summary="Keys rejected by Razorpay.",
        dedupe_key="cfg:1",
        detail={"key_secret": "must-not-appear"},
    )

    assert "must-not-appear" not in str(alert.detail)


# ── a condition that clears itself ──────────────────────────────────────────


def test_a_cleared_condition_can_be_resolved_by_its_key(db_session):
    """A detector that raises must be able to stand down.

    A kiosk that was offline for ten minutes and came back leaves an open alert
    nobody will close by hand, and a console of stale rows is the wall of
    identical notifications this table exists to avoid -- reached by a different
    road. The detector knows the dedupe key; it does not know the public id.
    """
    offline(db_session)

    resolved = resolve_by_key(db_session, dedupe_key="kiosk.offline:ksk_1")

    assert resolved is not None
    assert resolved.resolved is True
    assert open_alerts(db_session) == []


def test_standing_down_a_condition_nobody_raised_is_not_an_error(db_session):
    """The ordinary case: the sweep runs, everything is fine, nothing to close."""
    assert resolve_by_key(db_session, dedupe_key="kiosk.offline:ksk_1") is None


def test_standing_down_does_not_reach_an_alert_somebody_already_closed(db_session, operator):
    raised = offline(db_session)
    resolve(db_session, public_id=raised.public_id, actor_user_id=operator.id)

    resolve_by_key(db_session, dedupe_key="kiosk.offline:ksk_1")

    assert raised.resolved_by_user_id == operator.id


def test_a_condition_that_comes_back_after_standing_down_is_a_new_alert(db_session):
    first = offline(db_session)
    resolve_by_key(db_session, dedupe_key="kiosk.offline:ksk_1")

    second = offline(db_session)

    assert second.id != first.id
    assert second.occurrences == 1
