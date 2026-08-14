"""Every route must declare who may call it.

This test exists to fail. When someone adds a route and does not add it to
MATRIX, the build breaks and they are forced to state the authorisation rule
rather than inherit whatever the surrounding router happened to do. That is the
mechanism the old backend lacked, and why /owner/* ended up carrying a
"DO NOT LOOSEN" comment instead of a check.
"""

import pytest
from fastapi.routing import APIRoute

from app.core.config import Settings
from app.main import create_app
from tests.authz.matrix import KNOWN_AUDIENCES, MATRIX

IGNORED_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY="x" * 32,
    SECRETS_ENCRYPTION_KEY="k" * 44,
    CORS_ORIGINS="http://localhost:3000",
)


def declared_routes() -> list[tuple[str, str]]:
    app = create_app(SETTINGS)
    found = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in IGNORED_PATHS:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, route.path))
    return sorted(found)


def test_the_harness_sees_some_routes_at_all():
    """A collector that silently finds nothing would make every check below vacuous."""
    assert declared_routes(), "Route collection returned nothing — the harness is broken"


def test_every_route_has_a_matrix_entry():
    missing = [route for route in declared_routes() if route not in MATRIX]
    assert not missing, (
        "These routes have no authorisation matrix entry. Add them to "
        f"tests/authz/matrix.py and state who may call them: {missing}"
    )


def test_matrix_has_no_entries_for_routes_that_no_longer_exist():
    routes = set(declared_routes())
    stale = [entry for entry in MATRIX if entry not in routes]
    assert not stale, f"Matrix entries for routes that do not exist: {stale}"


@pytest.mark.parametrize("route", declared_routes())
def test_matrix_entry_is_a_known_audience_set(route):
    # A missing entry is already reported, with a useful message, by
    # test_every_route_has_a_matrix_entry. Skipping here keeps one mistake from
    # also producing a bare KeyError that buries the real message.
    if route not in MATRIX:
        pytest.skip("no matrix entry; reported by test_every_route_has_a_matrix_entry")

    allowed = MATRIX[route]
    assert allowed, f"{route} declares an empty audience set"
    assert allowed <= KNOWN_AUDIENCES, f"{route} names an unknown audience"
