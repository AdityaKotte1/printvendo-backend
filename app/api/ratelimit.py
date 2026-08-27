"""Abuse ceilings, in one table, applied before a request reaches a route.

**A table and a middleware, not a decorator per route.** A decorator is a thing
somebody has to remember; the missing one looks exactly like a route that was
considered and left open. Here the rules are a table, `tests/api/test_rate_limits`
reads the authz matrix to find every route reachable without a credential, and
one that appears in neither `LIMITS` nor `UNLIMITED` fails the build. Leaving a
route unlimited stays possible -- it just has to be said out loud, with a reason.

**Two buckets, and which one is tight depends on what we know.** A campus
shares one public address: two hundred students behind one NAT arrive from the
same IP, so a per-address limit tight enough to stop one script would lock out a
lecture hall -- and a loose one lets that script spend everybody else's budget.

So a request carrying a **verified** token is counted against its *account* at
the numbers below, and against its address at `ADDRESS_FANOUT` times them. One
student's script now spends its own budget; the hall is untouched; and a single
machine still cannot do as much as it likes by rotating a claim.

The token must be verified. Keying on an unchecked `sub` would hand an attacker
an unlimited supply of buckets for the price of editing a claim, which is worse
than having no per-account limit at all. An unverifiable token falls back to the
address -- which is also what the request is about to be refused for anyway.

What is still per-address only is **sign-in itself**, and unavoidably: there is
no token yet, and the account is in the request body, which this middleware
deliberately does not read. That is the right control for the shape of attack
that matters there -- credential stuffing rotates accounts from one machine, so
the address is the thing worth bounding.

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
from app.core.security import TokenError, TokenType, decode_token

logger = logging.getLogger(__name__)

TOO_MANY = "Too many attempts. Please wait a moment and try again."

# `X-Forwarded-For` when there is no proxy in front and no header to read.
UNKNOWN_CALLER = "unknown"

_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}

# How many accounts one address may be busy on before it is the address that
# looks like the problem. Sized as a lecture hall: twenty people printing from
# the campus wifi is a Tuesday, and one machine holding twenty accounts open is
# not. It multiplies whatever the per-account limit is, so the two numbers can
# never drift apart -- there is one table of ceilings, not two.
ADDRESS_FANOUT = 20


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


def account_key(headers: Mapping[str, str], *, secret: str) -> str | None:
    """The account this request is *provably* from, or nothing.

    Verified rather than merely decoded. The signature is the whole value of
    this: an unchecked `sub` is a field the caller controls, and a bucket keyed
    on a field the caller controls is not a limit.

    Cheap enough for the edge -- one HMAC, no database -- and a failure is not
    worth reporting here. A request with a bad token is refused by
    authentication a moment later; all this decides is which bucket it spent.
    """
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None

    try:
        return decode_token(token.strip(), TokenType.ACCESS, secret).subject
    except TokenError:
        return None


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

    def __init__(
        self,
        app,
        *,
        counter: Counter,
        trust_proxy: bool,
        secret: str = "",
        rules=None,
    ) -> None:
        self.app = app
        self.counter = counter
        self.trust_proxy = trust_proxy
        # Only to verify a bearer token's signature, so an account bucket is
        # keyed on something the caller cannot choose.
        self.secret = secret
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

        headers = Headers(scope=scope)
        client = scope.get("client")
        address = client_key(
            headers=headers,
            peer=client[0] if client else None,
            trust_proxy=self.trust_proxy,
        )
        account = account_key(headers, secret=self.secret) if self.secret else None

        route = f"{scope['method']} {scope['path']}"

        # The buckets this request spends, tightest first. An anonymous caller
        # has one; an authenticated one has its own plus a looser share of the
        # address it came from. Prefixed, so an account id can never land in the
        # same bucket as an address that happens to read like one.
        for limit, window_seconds in rules:
            buckets = (
                [(f"{route}|acct:{account}", limit),
                 (f"{route}|addr:{address}", limit * ADDRESS_FANOUT)]
                if account is not None
                else [(f"{route}|addr:{address}", limit)]
            )

            for key, ceiling in buckets:
                decision = await self.counter.hit(
                    key, limit=ceiling, window_seconds=window_seconds
                )
                if decision.allowed:
                    continue

                logger.info(
                    "rate limited %s for %s",
                    route,
                    account or address,
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
        secret=settings.JWT_SECRET_KEY,
    )
