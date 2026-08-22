"""Telling one kiosk's device that there is work, from any worker.

The constraint this lifts: the backend being replaced held its device registry
in a per-process dictionary, so a job created by one worker could never reach a
socket held by another. It had to run one worker forever. The Dockerfile here
already says `--workers 4`, on the strength of this module existing.

**A wake carries no work.** It is a hint that something is queued; the device
then claims through the ordinary HTTP path, which is a single
`FOR UPDATE SKIP LOCKED` statement. Pushing the task itself down the socket
would be a second implementation of claiming, and two devices could be handed
the same job the moment a reconnect overlapped a publish. The socket makes the
existing path prompt; it does not become a new one.

Two clients, deliberately. Publishing happens inside ordinary synchronous
request handlers, where awaiting is not available; listening happens in an async
WebSocket handler. One client cannot serve both without either blocking an event
loop or making every publisher async.

**Failure is not fatal.** A wake that cannot be published is logged and
swallowed: the device polls on its own schedule regardless, so the worst
outcome of a Redis outage is a job printing a few seconds later. Raising here
would take checkout down over an optimisation.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Protocol

logger = logging.getLogger(__name__)

# One channel per kiosk, so a delivery is routed by Redis rather than broadcast
# and filtered. A filtered broadcast would put every shop's job notifications
# through every connected Pi's process.
CHANNEL_PREFIX = "kiosk.wake"

WAKE = "wake"


class Bus(Protocol):
    """What the device socket needs, and nothing more."""

    def wake(self, kiosk_id: int) -> None: ...

    def wakeups(self, kiosk_id: int) -> AsyncIterator[str]: ...


class RedisBus:
    """The real bus, over Redis pub/sub.

    Takes its clients rather than building them, so a test can hand over
    `fakeredis` and exercise this code rather than a stand-in for it. The async
    client is built per subscription: a pub/sub connection is stateful and long
    lived, and sharing one between sockets would make one device's disconnect
    unsubscribe another.
    """

    def __init__(self, *, sync, async_factory: Callable[[], object]) -> None:
        self._sync = sync
        self._async_factory = async_factory

    @staticmethod
    def channel(kiosk_id: int) -> str:
        return f"{CHANNEL_PREFIX}.{kiosk_id}"

    def wake(self, kiosk_id: int) -> None:
        """Say that this kiosk has work waiting.

        Never raises. A kiosk with nothing listening is the ordinary case -- most
        are not connected at any given moment -- and a Redis that is down must
        not take an order down with it.
        """
        try:
            self._sync.publish(self.channel(kiosk_id), WAKE)
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.warning(
                "could not publish a wake for kiosk %s; it will poll instead",
                kiosk_id,
                exc_info=True,
            )

    @asynccontextmanager
    async def wakeups(self, kiosk_id: int) -> AsyncIterator[AsyncIterator[str]]:
        """Messages for this kiosk, for as long as the caller holds the context.

        A context manager rather than a bare iterator so the subscription and
        the connection are released when the socket goes away, however it goes
        away. A leaked pub/sub connection per dropped Pi is a slow leak nobody
        notices until the connection limit is reached.
        """
        client = self._async_factory()
        pubsub = client.pubsub()
        await pubsub.subscribe(self.channel(kiosk_id))

        async def stream() -> AsyncIterator[str]:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    # The subscribe confirmation, which is not a wake.
                    continue
                data = message.get("data")
                yield data.decode() if isinstance(data, bytes) else str(data)

        try:
            yield stream()
        finally:
            try:
                await pubsub.unsubscribe(self.channel(kiosk_id))
                await pubsub.aclose()
                await client.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("could not close a wake subscription", exc_info=True)


def redis_bus(url: str) -> RedisBus:
    """The bus a running app uses, built from `REDIS_URL`.

    Imported inside the function so that importing this module -- which
    `app.main` does at startup and pytest does at collection -- never requires
    the redis package to be importable in an environment that has no use for it.
    """
    import redis  # noqa: PLC0415
    import redis.asyncio as redis_async  # noqa: PLC0415

    return RedisBus(
        sync=redis.Redis.from_url(url),
        async_factory=lambda: redis_async.Redis.from_url(url),
    )


# What a transaction has queued work for, kept on the session until it commits.
# `Session.info` is a plain dict the ORM carries for exactly this: caller state
# that lives as long as the session and travels with it.
WAKE_KEY = "kiosks_to_wake"


def mark_for_wake(session, kiosk_id: int) -> None:
    """Note that this transaction has given a kiosk something to print.

    Called where the work is created rather than where the request ends, so a
    new route that queues a task cannot forget to wake the shop -- it does not
    know it is doing it. "Remember to call the helper" is what produced an audit
    trail covering 15 of 94 mutating routes in the backend being replaced.

    A set: an order with four documents queues four tasks at one kiosk, and
    four wakes would be four round trips saying the same thing.
    """
    session.info.setdefault(WAKE_KEY, set()).add(kiosk_id)


def flush_wakes(session, bus_for: Callable[[], Bus]) -> None:
    """Send the wakes this session accumulated, **after** its commit.

    Drained rather than read, so a second commit on the same session does not
    re-send the first one's wakes.

    Before the commit would be worse than useless: the device would ask, find
    nothing -- the task is not visible yet -- and go back to sleep, having spent
    the one notification it was going to get. After is the only correct moment.

    Takes a factory rather than a bus so that nothing is constructed on the
    overwhelming majority of requests, which queue no work at all.

    Never raises. The request has already committed by the time this runs, so a
    failure here would turn a paid and queued order into a 500 over a hint the
    device does not depend on.
    """
    pending = session.info.pop(WAKE_KEY, None)
    if not pending:
        return

    # The bus is built only once there is something to send. Every request goes
    # through here and almost none of them queue print work, so constructing a
    # client each time would be a connection pool per request for nothing.
    bus = bus_for()
    for kiosk_id in pending:
        try:
            bus.wake(kiosk_id)
        except Exception:  # noqa: BLE001 - see above
            logger.warning(
                "could not wake kiosk %s after commit; it will poll instead",
                kiosk_id,
                exc_info=True,
            )
