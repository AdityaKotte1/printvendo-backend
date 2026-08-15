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


def _flatten(routes) -> list[APIRoute]:
    """Every APIRoute an app serves, however it was attached.

    This has to recurse. `include_router` does not splice the child's routes
    into `app.routes`; it appends a wrapper object holding the original router.
    Filtering `app.routes` for APIRoute instances therefore sees only routes
    declared directly on the app and silently skips every mounted router --
    which is how all real routes are attached. That made this whole harness
    pass while checking nothing.
    """
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
            continue

        child = getattr(route, "original_router", None)
        if child is not None and hasattr(child, "routes"):
            found.extend(_flatten(child.routes))
        elif hasattr(route, "routes"):
            found.extend(_flatten(route.routes))
    return found


def declared_routes() -> list[tuple[str, str]]:
    app = create_app(SETTINGS)
    found = []
    for route in _flatten(app.routes):
        if route.path in IGNORED_PATHS:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, route.path))
    return sorted(found)


def test_the_harness_sees_some_routes_at_all():
    """A collector that silently finds nothing would make every check below vacuous."""
    assert declared_routes(), "Route collection returned nothing — the harness is broken"


def test_the_harness_sees_routes_attached_via_include_router():
    """Guards the exact bug this harness shipped with.

    Routes mounted with include_router live behind a wrapper object rather than
    directly in app.routes. If the collector stops following that indirection,
    every real route becomes invisible and the matrix silently protects
    nothing -- so assert on a route that is only reachable through a router.
    """
    routes = declared_routes()
    assert ("GET", "/v1/app/auth/me") in routes, routes
    assert ("GET", "/health") in routes, routes


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
