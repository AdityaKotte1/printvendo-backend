"""Counting hits per caller, in memory here and in Redis across four workers.

Both stores are driven through the same tests, because the whole point of the
pair is that they answer identically -- a limit that means one thing in dev and
another in production is not a limit anyone can reason about. The Redis one runs
against `fakeredis`, exactly as the wake bus does, so it is this code being
exercised rather than a stand-in for it.
"""

import asyncio

import pytest
from fakeredis import FakeAsyncRedis

from app.core.ratelimit import MemoryCounter, RedisCounter, counter_from_url

WINDOW = 60


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(params=["memory", "redis"])
def counter(request):
    if request.param == "memory":
        return MemoryCounter()
    return RedisCounter(client_factory=lambda: FakeAsyncRedis())


async def _hits(counter, key: str, times: int, *, limit: int, now: float):
    return [
        await counter.hit(key, limit=limit, window_seconds=WINDOW, now=now)
        for _ in range(times)
    ]


def test_a_caller_may_hit_exactly_the_limit(counter):
    decisions = _run(_hits(counter, "a", 3, limit=3, now=1000.0))

    assert [d.allowed for d in decisions] == [True, True, True]


def test_the_hit_after_the_limit_is_refused(counter):
    decisions = _run(_hits(counter, "a", 4, limit=3, now=1000.0))

    assert decisions[-1].allowed is False


def test_a_refusal_says_how_long_to_wait(counter):
    decisions = _run(_hits(counter, "a", 2, limit=1, now=1000.0))
    refused = decisions[-1]

    assert 0 < refused.retry_after <= WINDOW


def test_callers_are_counted_separately(counter):
    _run(_hits(counter, "a", 3, limit=3, now=1000.0))

    other = _run(_hits(counter, "b", 1, limit=3, now=1000.0))
    assert other[0].allowed is True


def test_the_next_window_starts_fresh(counter):
    _run(_hits(counter, "a", 3, limit=3, now=1000.0))

    later = _run(_hits(counter, "a", 1, limit=3, now=1000.0 + WINDOW))
    assert later[0].allowed is True


def test_an_allowed_hit_asks_nobody_to_wait(counter):
    decisions = _run(_hits(counter, "a", 1, limit=3, now=1000.0))

    assert decisions[0].retry_after == 0


# ── failure and construction ────────────────────────────────────────────────


class _Broken:
    """A Redis that is down, in the way a real one is: it raises."""

    def pipeline(self, *args, **kwargs):
        raise ConnectionError("redis is not there")


def test_a_store_that_is_down_lets_the_caller_through():
    """Fail open, deliberately.

    A Redis outage that refused every login would be an outage of the product.
    The limiter is an abuse ceiling, not an authorisation check -- nothing is
    protected by it that is not also protected by a password.
    """
    counter = RedisCounter(client_factory=_Broken)

    decision = _run(counter.hit("a", limit=1, window_seconds=WINDOW, now=1000.0))
    assert decision.allowed is True


def test_an_empty_url_gives_the_in_process_counter():
    assert isinstance(counter_from_url(""), MemoryCounter)


def test_a_redis_url_gives_the_shared_counter():
    assert isinstance(counter_from_url("redis://localhost:6379/0"), RedisCounter)


def test_memory_does_not_grow_without_bound():
    """One key per caller per window would otherwise be a slow leak."""
    counter = MemoryCounter()

    for i in range(50):
        _run(counter.hit(f"caller-{i}", limit=1, window_seconds=WINDOW, now=1000.0))
    _run(counter.hit("later", limit=1, window_seconds=WINDOW, now=1000.0 + WINDOW * 2))

    assert counter.tracked() == 1
