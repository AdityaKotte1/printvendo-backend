"""A world you can click through: a shop, its people, paper, and a printer slot.

Everything here goes through the ordinary services, so a seeded kiosk is subject
to every rule a real one is -- it climbs the onboarding ladder, its staff accept
invitations, and it cannot reach LIVE without being able to take a payment. A
seed that wrote rows directly would produce a world the product cannot produce,
and the first bug it found would be its own.
"""

import pytest
from cryptography.fernet import Fernet

from app.cli.seed import NOT_IN_PRODUCTION, seed_demo
from app.core.config import Settings
from app.core.errors import BadRequest, Conflict
from app.modules.identity import Role
from app.modules.identity import repository as identity_repo
from app.modules.kiosks import (
    AssignmentRole,
    OnboardingStage,
    list_staff,
    register_device,
    sheets_remaining,
)
from app.modules.wallet import balance_of


def _settings(env: str = "dev") -> Settings:
    return Settings(
        ENV=env,
        DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
        REDIS_URL="redis://localhost:6379/0",
        JWT_SECRET_KEY="s" * 32,
        SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        RAZORPAY_WEBHOOK_SECRET="whsec",
        CORS_ORIGINS="http://localhost:3000",
    )


@pytest.fixture
def world(db_session):
    return seed_demo(db_session, _settings())


# ── where it may run ────────────────────────────────────────────────────────


def test_it_refuses_to_run_in_production(db_session):
    """Two reasons, either one sufficient.

    A demo kiosk in production is a fake shop in every student's list. And at
    cutover it is a row the migration's reconciliation cannot account for --
    which is the one thing that has to balance.
    """
    with pytest.raises(BadRequest) as refused:
        seed_demo(db_session, _settings("prod"))

    assert NOT_IN_PRODUCTION in str(refused.value)


def test_it_runs_on_staging(db_session):
    """Staging is where the physical tests happen; it is the point."""
    assert seed_demo(db_session, _settings("staging")).kiosk is not None


def test_running_it_twice_says_what_to_do(db_session):
    seed_demo(db_session, _settings())

    with pytest.raises(Conflict) as refused:
        seed_demo(db_session, _settings())

    assert "--name" in str(refused.value)


# ── the shop ────────────────────────────────────────────────────────────────


def test_the_kiosk_is_live(world):
    """Climbed the whole ladder rather than being written in at the top."""
    assert world.kiosk.onboarding_stage is OnboardingStage.LIVE


def test_the_kiosk_has_prices(world):
    assert world.kiosk.price_bw_single > 0


def test_the_kiosk_has_paper(world, db_session):
    assert sheets_remaining(db_session, world.kiosk) > 0


def test_a_student_can_see_it(world, db_session):
    """The listing rule is `LIVE` and `can_take_payment`, so a seeded kiosk that
    fails the payment gate would be invisible and the tester would be stuck on
    an empty shop picker with nothing to read."""
    from app.api.student.kiosks import UNRESTRICTED
    from app.modules.kiosks import repository as kiosk_repo
    from app.modules.payments import can_take_payment

    listed = [
        kiosk
        for kiosk in kiosk_repo.list_kiosks(db_session, UNRESTRICTED)
        if kiosk.onboarding_stage is OnboardingStage.LIVE
        and can_take_payment(db_session, kiosk)
    ]
    assert world.kiosk in listed


# ── the people ──────────────────────────────────────────────────────────────


def test_the_owner_holds_the_role_and_the_shop(world, db_session):
    assert Role.OWNER in identity_repo.roles_of(db_session, world.owner.id)
    staff = dict((user.id, role) for user, role in list_staff(db_session, world.kiosk))
    assert staff[world.owner.id] is AssignmentRole.OWNER


def test_the_refiller_holds_the_role_and_the_shop(world, db_session):
    assert Role.REFILLER in identity_repo.roles_of(db_session, world.refiller.id)
    staff = dict((user.id, role) for user, role in list_staff(db_session, world.kiosk))
    assert staff[world.refiller.id] is AssignmentRole.REFILLER


def test_the_kiosk_takes_wallet_money(world):
    """The whole reason the seeded world is testable without Razorpay.

    A kiosk created and left at the default refuses wallet payment, so the first
    end-to-end run stops at "pay by card or UPI" -- which is exactly where the
    first one did stop.
    """
    assert world.kiosk.accepts_wallet is True


def test_the_student_has_money_to_spend(world, db_session):
    """Wallet-paid printing is testable without a card, which is what makes the
    first end-to-end run possible before Razorpay is wired to anything."""
    assert balance_of(db_session, user_id=world.student.id) > 0


def test_every_account_comes_with_a_password_to_sign_in_with(world):
    assert all(
        len(password) >= 12
        for password in (
            world.passwords[world.owner.email],
            world.passwords[world.refiller.email],
            world.passwords[world.student.email],
        )
    )


# ── the printer slot ────────────────────────────────────────────────────────


def test_the_enrolment_code_actually_enrols_a_device(world, db_session):
    """Handed out rather than spent, so the tester's Pi -- or their curl -- is
    the thing that becomes the device, which is what will happen in the shop."""
    issued = register_device(db_session, world.enrolment_code, agent_version="test")

    assert issued.token.startswith("dvt_")
