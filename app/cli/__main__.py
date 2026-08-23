"""`python -m app.cli <command>`.

Thin on purpose: parse, open one transaction, call one function, print what
happened. Everything it prints is a credential or an id somebody is about to
paste somewhere, so it prints them once, plainly, and does not log them.
"""

import argparse
import sys
from decimal import Decimal

from app.cli.bootstrap import bootstrap_admin
from app.cli.seed import DEFAULT_NAME, seed_demo
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import AppError, Conflict
from app.modules.identity import repository as identity_repo
from app.modules.identity.roles import Role
from app.modules.kiosks import KioskType
from app.provisioning import provision_kiosk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    admin = commands.add_parser(
        "bootstrap-admin",
        help="make the first administrator, on a system that has none",
    )
    admin.add_argument("--email", required=True)
    admin.add_argument("--password", default=None)
    admin.add_argument("--name", default=None)
    admin.add_argument(
        "--force",
        action="store_true",
        help="grant admin even though one already exists (recovery)",
    )

    seed = commands.add_parser(
        "seed", help="create a shop, its people, paper and a printer slot"
    )
    seed.add_argument("--name", default=DEFAULT_NAME, help="the kiosk's name")

    kiosk = commands.add_parser(
        "provision-kiosk",
        help="stand a real shop up: create it, price it, stock it, enrol it",
    )
    kiosk.add_argument("--name", required=True)
    kiosk.add_argument(
        "--type", default="platform", choices=[t.value for t in KioskType]
    )
    kiosk.add_argument(
        "--owner-email",
        default=None,
        help="invite this address to own the shop (required in practice for sold/saas)",
    )
    kiosk.add_argument("--bw-single", type=Decimal, default=None)
    kiosk.add_argument("--bw-double", type=Decimal, default=None)
    kiosk.add_argument("--color-single", type=Decimal, default=None)
    kiosk.add_argument("--color-double", type=Decimal, default=None)
    kiosk.add_argument("--location", default=None)
    kiosk.add_argument("--latitude", type=float, default=None)
    kiosk.add_argument("--longitude", type=float, default=None)
    kiosk.add_argument("--paper", type=int, default=None, help="tray size in sheets")

    args = parser.parse_args(argv)
    settings = get_settings()

    try:
        with session_scope(settings.DATABASE_URL) as db:
            if args.command == "bootstrap-admin":
                user = bootstrap_admin(
                    db,
                    email=args.email,
                    password=args.password,
                    full_name=args.name,
                    force=args.force,
                )
                print(f"admin: {user.email}  ({user.public_id})")
                if args.password:
                    print("password: the one you passed")
                return 0

            if args.command == "provision-kiosk":
                prices = {
                    field: value
                    for field, value in (
                        ("bw_single", args.bw_single),
                        ("bw_double", args.bw_double),
                        ("color_single", args.color_single),
                        ("color_double", args.color_double),
                    )
                    if value is not None
                }
                # The same function the admin route calls. Two implementations
                # of "set up a shop" is how one of them ends up skipping a rung.
                result = provision_kiosk(
                    db,
                    name=args.name,
                    kiosk_type=KioskType(args.type),
                    prices=prices,
                    actor_user_id=_an_admin(db),
                    location_description=args.location,
                    latitude=args.latitude,
                    longitude=args.longitude,
                    paper_capacity=args.paper,
                    owner_email=args.owner_email,
                )
                _report_kiosk(result, settings)
                return 0

            world = seed_demo(db, settings, name=args.name)
            _report(world, settings)
            return 0
    except AppError as refused:
        # The message is written for a person and says what to do instead, so
        # it is printed as it stands rather than wrapped in a traceback.
        print(f"refused: {refused.detail}", file=sys.stderr)
        return 1


def _an_admin(db) -> int:
    """Whose name the setup is recorded under.

    An audit row needs an actor, and the command line has no session. The first
    administrator stands in -- honest, because somebody with shell access is at
    least as privileged as an admin, and better than a null that reads as "the
    system did this" when a person plainly did.
    """
    admin = identity_repo.first_with_role(db, Role.ADMIN)
    if admin is None:
        raise Conflict(
            "There is no administrator yet. Run bootstrap-admin first: a shop "
            "has to be set up by somebody."
        )
    return admin.id


def _report_kiosk(result, settings) -> None:
    kiosk = result.kiosk
    print()
    print(f"kiosk    {kiosk.name}  {kiosk.public_id}  ({kiosk.onboarding_stage})")

    if result.blocked_by:
        print()
        print("not selling yet:")
        for reason in result.blocked_by:
            print(f"  - {reason}")
    else:
        print()
        print("selling: students can send jobs to it now.")

    if result.owner_invite_token is not None:
        print()
        print("the owner has been invited. They accept at:")
        link = f"{settings.APP_BASE_URL}/accept-invite?token={result.owner_invite_token}"
        print(f"  {link}")

    print()
    print(f"enrolment code (spend within 12h): {result.enrolment_code}")
    print("  on the machine itself:")
    print(f"  POST {settings.PUBLIC_BASE_URL}/v1/device/register")
    print(f'       {{"enrolment_code": "{result.enrolment_code}"}}')
    print()

def _report(world, settings) -> None:
    print(f"\nkiosk    {world.kiosk.name}  {world.kiosk.public_id}  (LIVE)")
    print("\nsign in with:")
    for email, password in world.passwords.items():
        print(f"  {email:44} {password}")

    print(f"\nenrolment code (spend within 12h): {world.enrolment_code}")
    print("  a Pi -- or a curl standing in for one -- becomes the device with:")
    print(
        f"""  curl -X POST {settings.PUBLIC_BASE_URL}/v1/device/register \\
       -H 'content-type: application/json' \\
       -d '{{"enrolment_code": "{world.enrolment_code}", "agent_version": "manual"}}'"""
    )
    print("\nthe student has money in their wallet, so printing needs no card.\n")


if __name__ == "__main__":
    raise SystemExit(main())
