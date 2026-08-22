"""Abuse ceilings, in one table, applied before a request reaches a route.

**A table and a middleware, not a decorator per route.** A decorator is a thing
somebody has to remember; the missing one looks exactly like a route that was
considered and left open. Here the rules are a table, `tests/api/test_rate_limits`
reads the authz matrix to find every route reachable without a credential, and
one that appears in neither `LIMITS` nor `UNLIMITED` fails the build. Leaving a
route unlimited stays possible -- it just has to be said out loud, with a reason.

**These are ceilings, not policy.** A campus shares one public address: two
hundred students behind one NAT all log in from the same IP, so a limit tight
enough to stop a targeted brute force would lock out a lecture hall. The numbers
below are set where a script is obviously a script and a busy campus is not.
Per-account limits -- the ones that actually stop credential stuffing on one
person -- need the email out of the request body, which is the route's business
rather than the edge's, and they are not built yet. Saying so here beats
implying this covers more than it does.

**The key is one address, and which one is a deployment fact.** Behind a reverse
proxy every request arrives from the proxy, so without `TRUST_PROXY_HEADERS`
the whole internet shares one bucket. With it, the address is the *last* entry
in `X-Forwarded-For` -- the peer our own proxy saw. Anything to the left of that
was supplied by the caller, so trusting the leftmost entry would let one client
mint a fresh bucket per request by prepending a made-up address.
"""

import logging
from collections.abc import Mapping

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.core.config import Settings
from app.core.ratelimit import Counter, counter_from_url

logger = logging.getLogger(__name__)

TOO_MANY = "Too many attempts. Please wait a moment and try again."

# `X-Forwarded-For` when there is no proxy in front and no header to read.
UNKNOWN_CALLER = "unknown"

_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}


# Per caller. A tuple is several windows on the same route: a burst ceiling and
# a sustained one, which are different questions -- twenty in a minute is a
# person retrying, two hundred in an hour is not.
LIMITS: dict[tuple[str, str], tuple[str, ...]] = {
    # ── credentials ─────────────────────────────────────────────────────────
    ("POST", "/v1/app/auth/login"): ("30/minute", "300/hour"),
    ("POST", "/v1/app/auth/register"): ("20/minute", "200/hour"),
    ("POST", "/v1/app/auth/guest"): ("30/minute", "300/hour"),
    ("POST", "/v1/app/auth/google"): ("30/minute", "300/hour"),
    ("POST", "/v1/app/auth/refresh"): ("120/minute",),
    ("POST", "/v1/app/auth/logout"): ("120/minute",),
    ("POST", "/v1/app/auth/change-password"): ("20/minute",),
    # ── tokens somebody might guess ─────────────────────────────────────────
    # A reset token, a verification token and an invitation token are all
    # credentials that arrive in a body. Guessing one is the attack these
    # bound; the tokens themselves are long enough that a bound is a formality,
    # which is the right order to have those two properties in.
    ("POST", "/v1/app/auth/verify-email"): ("30/minute",),
    ("POST", "/v1/app/auth/reset-password"): ("20/minute",),
    ("POST", "/v1/app/staff/accept-invite"): ("20/minute",),
    # A one-time enrolment code, spent by a Pi during an install. One machine
    # per shop, installed by hand -- nothing legitimate here is in a hurry.
    ("POST", "/v1/device/register"): ("10/minute", "60/hour"),
    # ── routes that send email ──────────────────────────────────────────────
    # Each of these puts a message in somebody's inbox at our expense. The
    # hourly figure is the one that matters: a mail bomb is sustained.
    ("POST", "/v1/app/auth/forgot-password"): ("10/minute", "60/hour"),
    ("POST", "/v1/app/auth/resend-verification"): ("10/minute", "60/hour"),
}


WEBHOOK = (
    "Razorpay retries a delivery it cannot get through, so a refused webhook "
    "comes back rather than going away. Throttling one would delay settling "
    "money we have already taken, and the signature check is the real gate."
)
PROBE = (
    "A liveness probe that gets throttled reads as an outage, and takes the "
    "service out of rotation to prove it."
)

# Public and deliberately unlimited. The reason is the point: a route in neither
# table is an oversight, and one here is an argument somebody can disagree with.
UNLIMITED: dict[tuple[str, str], str] = {
    ("GET", "/health"): PROBE,
    ("POST", "/v1/webhooks/razorpay"): WEBHOOK,
    ("POST", "/v1/webhooks/razorpay/{owner_id}"): WEBHOOK,
}


def client_key(
    *, headers: Mapping[str, str], peer: str | None, trust_proxy: bool
) -> str:
    """Which caller this is, as far as the edge can tell."""
    if trust_proxy:
        forwarded = headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return peer or UNKNOWN_CALLER


def _parse(limit: str) -> tuple[int, int]:
    """"30/minute" -> (30, 60). Raises at startup rather than at request time."""
    count, _, period = limit.partition("/")
    if period not in _PERIODS:
        raise ValueError(f"{limit!r} is not a rate limit: {sorted(_PERIODS)}")
    return int(count), _PERIODS[period]


class RateLimitMiddleware:
    """Pure ASGI, so a refused request costs no route, no session, no query.

    That is the whole reason it is middleware rather than a dependency: a
    dependency runs after routing has resolved the path and after FastAPI has
    begun building the handler's arguments -- including, for most of these
    routes, a database session.
    """

    def __init__(self, app, *, counter: Counter, trust_proxy: bool, rules=None) -> None:
        self.app = app
        self.counter = counter
        self.trust_proxy = trust_proxy
        self.rules = {
            route: tuple(_parse(limit) for limit in limits)
            for route, limits in (LIMITS if rules is None else rules).items()
        }

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The raw path, because routing has not happened yet. Every rule is a
        # literal path for that reason, and a test enforces it.
        rules = self.rules.get((scope["method"], scope["path"]))
        if not rules:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        caller = client_key(
            headers=Headers(scope=scope),
            peer=client[0] if client else None,
            trust_proxy=self.trust_proxy,
        )

        for limit, window_seconds in rules:
            decision = await self.counter.hit(
                f"{scope['method']} {scope['path']}|{caller}",
                limit=limit,
                window_seconds=window_seconds,
            )
            if not decision.allowed:
                logger.info(
                    "rate limited %s %s for %s", scope["method"], scope["path"], caller
                )
                response = JSONResponse(
                    status_code=429,
                    content={"detail": TOO_MANY},
                    headers={"Retry-After": str(decision.retry_after)},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


def install_rate_limiting(app: FastAPI, settings: Settings) -> None:
    """Wire the limiter, unless it has been switched off.

    Must be added *before* the CORS middleware: the last middleware added is
    the outermost, and a 429 raised outside CORS carries no
    `Access-Control-Allow-Origin` -- so a browser reports it as a network
    failure rather than as the refusal it is, and the app shows the wrong thing
    to the person being limited.
    """
    if not settings.RATE_LIMIT_ENABLED:
        logger.warning("rate limiting is switched off")
        return

    app.add_middleware(
        RateLimitMiddleware,
        counter=counter_from_url(settings.rate_limit_store_url),
        trust_proxy=settings.TRUST_PROXY_HEADERS,
    )
