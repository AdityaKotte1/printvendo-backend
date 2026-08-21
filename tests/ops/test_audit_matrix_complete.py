"""Every mutating route must decide whether it leaves a trail.

This test exists to fail. Add a POST, PUT, PATCH or DELETE without an entry in
AUDIT_MATRIX and the build breaks, so the decision gets made in review rather
than by default. The old backend's audit helper was correct and was called from
15 of its 94 mutating routes -- the gap was never in the helper, it was that
nothing noticed the other 79.

The harness deliberately mirrors `tests/authz/test_matrix_complete.py`,
including its route collector: `include_router` does not splice a child's routes
into `app.routes`, so filtering for APIRoute at the top level silently finds
almost nothing. That bug made the authz harness pass while checking an empty
set, and the same shape of mistake here would be invisible in exactly the same
way -- hence the first two tests below, which check the harness before the
harness checks anything.
"""

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.main import create_app
from tests.authz.test_matrix_complete import _flatten
from tests.ops.audit_matrix import AUDIT_MATRIX, AUDITED, EXEMPT

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
IGNORED_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY="x" * 32,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="http://localhost:3000",
)


def mutating_routes() -> list[tuple[str, str]]:
    app = create_app(SETTINGS)
    found = []
    for route in _flatten(app.routes):
        if route.path in IGNORED_PATHS:
            continue
        for method in sorted(route.methods & MUTATING):
            found.append((method, route.path))
    return sorted(found)


def test_the_harness_finds_mutating_routes_at_all():
    """A collector that finds nothing would make every check below vacuous, and
    would look exactly like a clean build."""
    found = mutating_routes()
    assert len(found) > 20, f"Only found {len(found)} mutating routes — harness broken"


def test_the_harness_finds_routes_from_mounted_routers():
    """Every real route is attached with include_router. If the collector only
    saw top-level ones it would find /health and nothing else."""
    found = mutating_routes()
    assert ("POST", "/v1/app/orders") in found


def test_every_mutating_route_has_decided_about_audit():
    found = set(mutating_routes())
    declared = set(AUDIT_MATRIX)

    undeclared = found - declared
    assert not undeclared, (
        "These mutating routes do not say whether they are audited. Add each to "
        "tests/ops/audit_matrix.py as AUDITED, or as EXEMPT with a named "
        f"reason: {sorted(undeclared)}"
    )


def test_the_matrix_does_not_describe_routes_that_are_gone():
    """A stale entry is a claim about coverage that is no longer true."""
    found = set(mutating_routes())
    stale = set(AUDIT_MATRIX) - found
    assert not stale, f"These entries name routes that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("route", sorted(AUDIT_MATRIX))
def test_each_entry_is_well_formed(route):
    decision, reason = AUDIT_MATRIX[route]
    assert decision in {AUDITED, EXEMPT}, f"{route}: unknown decision {decision!r}"
    if decision == EXEMPT:
        assert reason, f"{route}: EXEMPT needs a reason someone can argue with"


def test_something_is_actually_audited():
    """A matrix where everything is EXEMPT would pass every check above while
    meaning the audit log is switched off."""
    audited = [r for r, (d, _) in AUDIT_MATRIX.items() if d == AUDITED]
    assert len(audited) >= 5, f"Only {len(audited)} routes are audited at all"
