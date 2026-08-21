"""The audit trail, and what must never end up in it.

The old backend's helper was correct and under-called. These tests cover the
helper's own rules -- transactional behaviour, secret redaction, money that
survives the round trip -- and then check that the one place paper changes pass
through actually writes an entry, because a helper nobody calls is the failure
mode this module exists to prevent.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper
from app.modules.kiosks.paper import reset_paper, set_paper
from app.modules.ops import entries_for, record
from app.modules.ops.audit import REDACTED, scrub
from app.modules.ops.models import AuditEntry


@pytest.fixture
def actor(db_session) -> User:
    user = User(email="auditor@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(
        name="Audited Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=200))
    db_session.flush()
    return kiosk


# ── the entry ───────────────────────────────────────────────────────────────


def test_an_entry_records_who_did_what_to_which_thing(db_session, actor):
    record(
        db_session,
        action="kiosk.pricing.update",
        entity_type="kiosk",
        entity_id="ksk_abc",
        actor_user_id=actor.id,
        before={"bw_single": Decimal("2.00")},
        after={"bw_single": Decimal("3.00")},
    )
    db_session.flush()

    entry = db_session.execute(select(AuditEntry)).scalar_one()
    assert entry.action == "kiosk.pricing.update"
    assert entry.entity_id == "ksk_abc"
    assert entry.actor_user_id == actor.id


def test_money_survives_the_round_trip_exactly(db_session, actor):
    """JSON has no decimal type. Storing money as a float is how the record of
    what a price *used to be* stops being trustworthy."""
    record(
        db_session,
        action="kiosk.pricing.update",
        entity_type="kiosk",
        before={"price": Decimal("2.10")},
        after={"price": Decimal("19.99")},
    )
    db_session.flush()
    db_session.expire_all()

    entry = db_session.execute(select(AuditEntry)).scalar_one()
    assert entry.before["price"] == "2.10"
    assert Decimal(entry.after["price"]) == Decimal("19.99")


def test_the_system_acting_is_recorded_as_nobody_not_as_missing(db_session):
    """A scheduled expiry has no actor. "Nobody did this" is a different fact
    from "we failed to record who", and only one of them is true here."""
    record(db_session, action="order.expired", entity_type="order", entity_id="ord_1")
    db_session.flush()

    assert db_session.execute(select(AuditEntry)).scalar_one().actor_user_id is None


# ── secrets never land here ─────────────────────────────────────────────────


def test_a_secret_is_redacted_however_deep_it_is(db_session):
    """Auditing that an owner changed their Razorpay keys is the point.
    Auditing the keys themselves would be a second plaintext copy of a
    credential, in a table admins are allowed to read."""
    record(
        db_session,
        action="payment_config.update",
        entity_type="user",
        after={
            "razorpay_key_id": "rzp_live_visible",
            "razorpay_key_secret": "must-not-appear",
            "nested": {"webhook_secret": "also-must-not-appear"},
            "list": [{"password": "nor-this"}],
        },
    )
    db_session.flush()

    entry = db_session.execute(select(AuditEntry)).scalar_one()
    rendered = str(entry.after)
    assert "must-not-appear" not in rendered
    assert "also-must-not-appear" not in rendered
    assert "nor-this" not in rendered
    # The non-secret half is still there, because that is what makes the entry
    # useful: which key id they moved to.
    assert entry.after["razorpay_key_id"] == "rzp_live_visible"
    assert entry.after["razorpay_key_secret"] == REDACTED


def test_redaction_is_case_insensitive():
    assert scrub({"Password": "x"})["Password"] == REDACTED
    assert scrub({"KEY_SECRET": "x"})["KEY_SECRET"] == REDACTED


# ── it lands with the change, or not at all ─────────────────────────────────


def test_an_entry_is_discarded_when_the_change_it_describes_rolls_back(
    db_session, actor, kiosk
):
    """The entry and the change are one transaction, because `record` never
    commits. A mutation that fails afterwards must not leave a trail claiming
    something happened that did not.

    Driven through a savepoint rather than by inspecting the session: querying
    autoflushes, so "has it been written yet" is not a question a test can ask
    without changing the answer. What matters is not whether the INSERT has been
    sent, it is whether it survives a rollback.
    """
    with pytest.raises(RuntimeError):
        with db_session.begin_nested():
            reset_paper(db_session, kiosk, actor_user_id=actor.id)
            raise RuntimeError("the mutation failed after auditing")

    assert entries_for(db_session, entity_type="kiosk") == []


# ── the helper is actually called ───────────────────────────────────────────


def test_resetting_paper_leaves_a_trail(db_session, actor, kiosk):
    """Auditing lives inside the one function every paper change passes
    through, so reset, set and out-of-paper are covered from both the owner and
    the refiller router by one implementation."""
    reset_paper(db_session, kiosk, actor_user_id=actor.id)
    db_session.flush()

    entries = entries_for(db_session, entity_type="kiosk", entity_id=kiosk.public_id)
    assert [e.action for e in entries] == ["kiosk.paper.reset"]
    assert entries[0].actor_user_id == actor.id
    assert entries[0].before["used"] == 200
    assert entries[0].after["used"] == 0


def test_setting_paper_leaves_a_trail_naming_the_person(db_session, actor, kiosk):
    """"Who reset the tray to 500 when there were clearly 200 sheets in it" is
    the question this answers."""
    set_paper(db_session, kiosk, sheets_left=500, actor_user_id=actor.id, note="refill")
    db_session.flush()

    entry = entries_for(db_session, action="kiosk.paper.set")[0]
    assert entry.actor_user_id == actor.id
    assert entry.entity_id == kiosk.public_id
    assert entry.note == "refill"


def test_the_trail_can_be_read_back_by_entity(db_session, actor, kiosk):
    reset_paper(db_session, kiosk, actor_user_id=actor.id)
    set_paper(db_session, kiosk, sheets_left=100, actor_user_id=actor.id)
    db_session.flush()

    assert len(entries_for(db_session, entity_type="kiosk", entity_id=kiosk.public_id)) == 2
    assert entries_for(db_session, entity_id="ksk_nothing") == []
