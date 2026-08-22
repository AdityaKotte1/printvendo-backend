"""Waking one kiosk's device, from whichever worker created the work.

The constraint this exists to lift: the backend being replaced kept its device
registry in a per-process dict, so a job created by one worker could never reach
a socket held by another. It therefore had to run a single worker forever, and
the Dockerfile here already claims `--workers 4` on the strength of this.

Exercised against `fakeredis`, which speaks the same pub/sub protocol as the
real thing. Real Redis is exercised at staging -- it is not installed locally,
and the operator confirmed that is a production concern rather than a local one.
"""

import asyncio

from fakeredis import FakeAsyncRedis, FakeServer, FakeStrictRedis

from app.core.bus import RedisBus, flush_wakes, mark_for_wake


class _FakeSession:
    """Just the `info` dict, which is all this mechanism touches."""

    def __init__(self) -> None:
        self.info: dict = {}


def _bus() -> RedisBus:
    """One fake Redis server behind both clients.

    The two clients over one server is what makes these real tests of the
    arrangement rather than of a stand-in: publish goes through the synchronous
    client a request handler uses, and the socket listens on the async one.
    """
    server = FakeServer()
    return RedisBus(
        sync=FakeStrictRedis(server=server),
        async_factory=lambda: FakeAsyncRedis(server=server),
    )


async def _first_wakeup(bus: RedisBus, kiosk_id: int, *, publish_after: float = 0.05):
    """Subscribe, publish once the subscription is live, take one message."""

    async def publish_soon() -> None:
        await asyncio.sleep(publish_after)
        bus.wake(kiosk_id)

    async with bus.wakeups(kiosk_id) as stream:
        task = asyncio.create_task(publish_soon())
        try:
            return await asyncio.wait_for(anext(stream), timeout=2)
        finally:
            task.cancel()


def test_a_wake_reaches_a_listener_on_that_kiosk():
    bus = _bus()

    assert asyncio.run(_first_wakeup(bus, 7)) is not None


def test_a_wake_for_another_kiosk_is_not_delivered():
    """One shop's Pi must not be woken by another shop's job. The channel is the
    isolation, so this is the test that it is really per kiosk rather than a
    broadcast everyone filters."""
    bus = _bus()

    async def scenario() -> bool:
        async with bus.wakeups(7) as stream:
            await asyncio.sleep(0.05)
            bus.wake(8)
            try:
                await asyncio.wait_for(anext(stream), timeout=0.3)
            except TimeoutError:
                return True
            return False

    assert asyncio.run(scenario()) is True


def test_publishing_with_nobody_listening_is_not_an_error():
    """Ordinary, not exceptional: most kiosks are not connected at any moment,
    and a job created for one of them must still commit. A wake is a hint that
    work exists, never the delivery mechanism for the work itself."""
    bus = _bus()

    bus.wake(999)


def test_a_channel_names_exactly_one_kiosk():
    """Guards the isolation above at the point it is decided, so the rule does
    not rest on a test that could pass by timing."""
    assert RedisBus.channel(7) != RedisBus.channel(8)
    assert "7" in RedisBus.channel(7)


# ── waking whoever a transaction gave work to ───────────────────────────────
# The rule this arrangement exists for: a wake fires **after the commit that
# created the task**, and it fires without any route remembering to send it.
# Three routes queue print work today, and "remember to call the helper" is
# exactly what produced an audit trail covering 15 of 94 mutating routes.


def test_a_kiosk_marked_during_a_transaction_is_woken_when_it_is_flushed():
    bus = _bus()
    published: list[int] = []
    bus.wake = published.append  # type: ignore[method-assign]

    session = _FakeSession()
    mark_for_wake(session, 7)
    flush_wakes(session, lambda: bus)

    assert published == [7]


def test_the_same_kiosk_twice_in_one_transaction_is_one_wake():
    """An order with four documents queues four tasks at one kiosk. Four wakes
    would be four round trips to tell a device the same thing once."""
    bus = _bus()
    published: list[int] = []
    bus.wake = published.append  # type: ignore[method-assign]

    session = _FakeSession()
    for _ in range(4):
        mark_for_wake(session, 7)
    flush_wakes(session, lambda: bus)

    assert published == [7]


def test_flushing_twice_does_not_wake_twice():
    """The marks are drained, not read. A second flush after a second commit in
    the same session must not re-send the first one's wakes."""
    bus = _bus()
    published: list[int] = []
    bus.wake = published.append  # type: ignore[method-assign]

    session = _FakeSession()
    mark_for_wake(session, 7)
    flush_wakes(session, lambda: bus)
    flush_wakes(session, lambda: bus)

    assert published == [7]


def test_flushing_a_transaction_that_queued_nothing_does_nothing():
    bus = _bus()
    published: list[int] = []
    bus.wake = published.append  # type: ignore[method-assign]

    flush_wakes(_FakeSession(), lambda: bus)

    assert published == []


def test_a_wake_that_cannot_be_sent_never_reaches_the_caller():
    """The request has already committed by the time this runs. Raising here
    would turn a successful, paid, queued order into a 500 -- over a hint the
    device does not need, because it polls anyway."""

    class Broken:
        def wake(self, kiosk_id: int) -> None:
            raise RuntimeError("redis is gone")

    session = _FakeSession()
    mark_for_wake(session, 7)

    flush_wakes(session, Broken)
