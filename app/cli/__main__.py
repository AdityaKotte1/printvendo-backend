"""`python -m app.cli <command>`.

Thin on purpose: parse, open one transaction, call one function, print what
happened. Everything it prints is a credential or an id somebody is about to
paste somewhere, so it prints them once, plainly, and does not log them.
"""

import argparse
import sys

from app.cli.bootstrap import bootstrap_admin
from app.cli.seed import DEFAULT_NAME, seed_demo
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import AppError


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

            world = seed_demo(db, settings, name=args.name)
            _report(world, settings)
            return 0
    except AppError as refused:
        # The message is written for a person and says what to do instead, so
        # it is printed as it stands rather than wrapped in a traceback.
        print(f"refused: {refused.detail}", file=sys.stderr)
        return 1


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
