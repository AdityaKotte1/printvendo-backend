"""Counting how often one caller has done one thing, in a fixed window.

This is deliberately not `slowapi`. Two things pushed against it: its decorator
form puts the limit on each route, which is the "remember to call the helper"
shape that left the old backend's audit trail covering 15 of 94 routes; and its
async storage needs `coredis`, a second Redis client beside the `redis` one the
wake bus already uses. Two clients for the same server, to count integers.

**One algorithm, two stores.** The window is aligned to the clock -- the key
carries `int(now / window)` -- so the counter never has to expire anything by
hand and both stores answer identically. The cost of an aligned window is that a
caller can spend a full allowance either side of a boundary; against an abuse
ceiling of twenty a minute that is not a difference anyone can use.

**Failure is not fatal.** A store that is down lets the caller through, and says
so in the log. Refusing every login because Redis restarted would be an outage
of the product to protect it from an abuse that may not be happening. Nothing
here is an authorisation check: everything it guards is also guarded by a
password, a token or a signature.
"""

import logging
import time
from dataclasses import dataclass
from math import ceil
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    """Whether this hit is allowed, and when to come back if not."""

    allowed: bool
    retry_after: int = 0


ALLOWED = Decision(allowed=True)


def _window_of(now: float, window_seconds: int) -> tuple[int, float]:
    """Which aligned window `now` falls in, and when it ends."""
    index = int(now // window_seconds)
    return index, float((index + 1) * window_seconds)


def _refusal(now: float, ends_at: float) -> Decision:
    # Never zero: a client told to retry after 0 seconds retries immediately and
    # is refused again, which reads as a broken endpoint rather than a limit.
    return Decision(allowed=False, retry_after=max(1, ceil(ends_at - now)))


class Counter(Protocol):
    """What the rate limiter needs of a store, and nothing more."""

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, now: float | None = None
    ) -> Decision: ...


class MemoryCounter:
    """Per process, for development and tests.

    Correct for one worker and honest about being wrong for four: with
    `--workers 4` each process holds its own counts, so the effective ceiling is
    four times what is configured. That is why production is handed a Redis URL
    and why `counter_from_url` is the only place that chooses.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, int, int], tuple[int, float]] = {}

    def tracked(self) -> int:
        """How many live counters are held. Exists so a test can prove the
        sweep runs; nothing in the app calls it."""
        return len(self._counts)

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, now: float | None = None
    ) -> Decision:
        now = _now(now)
        index, ends_at = _window_of(now, window_seconds)

        self._sweep(now)

        bucket = (key, window_seconds, index)
        count, _ = self._counts.get(bucket, (0, ends_at))
        if count >= limit:
            return _refusal(now, ends_at)

        self._counts[bucket] = (count + 1, ends_at)
        return ALLOWED

    def _sweep(self, now: float) -> None:
        """Drop every counter whose window has ended.

        On every hit, and O(the number of live callers) for it. That is only
        acceptable because this store is never the one under load: production
        counts in Redis, which expires keys itself. Without the sweep, one entry
        per caller per window is a slow leak in a long-running dev server.
        """
        self._counts = {
            bucket: value for bucket, value in self._counts.items() if value[1] > now
        }


class RedisCounter:
    """Shared by every worker, which is what makes the configured number true.

    Takes a client factory rather than a client so a test can hand over
    `fakeredis` and drive this code, and so nothing connects at import time.
    """

    def __init__(self, *, client_factory) -> None:
        self._client_factory = client_factory
        self._client = None

    def _client_or_connect(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, now: float | None = None
    ) -> Decision:
        now = _now(now)
        index, ends_at = _window_of(now, window_seconds)
        redis_key = f"rl:{key}:{window_seconds}:{index}"

        try:
            client = self._client_or_connect()
            pipeline = client.pipeline()
            pipeline.incr(redis_key)
            # Two windows' worth: the key is window-scoped, so the expiry only
            # has to outlive the window it belongs to. Setting it on every hit
            # is what makes this safe against a lost EXPIRE.
            pipeline.expire(redis_key, window_seconds * 2)
            count, _ = await pipeline.execute()
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.warning(
                "rate limit store is unavailable; allowing the request", exc_info=True
            )
            return ALLOWED

        if int(count) > limit:
            return _refusal(now, ends_at)
        return ALLOWED


def counter_from_url(url: str) -> Counter:
    """The counter a running app uses. The only place the choice is made."""
    if not url.strip():
        return MemoryCounter()

    def connect():
        import redis.asyncio as redis_async  # noqa: PLC0415

        return redis_async.Redis.from_url(url)

    return RedisCounter(client_factory=connect)


def _now(now: float | None) -> float:
    return time.time() if now is None else now
