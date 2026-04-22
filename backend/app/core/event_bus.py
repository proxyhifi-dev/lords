from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from time import time
from typing import Any, AsyncIterator

from backend.app.utils.logger import get_logger

logger = get_logger("event_bus")


# ------------------------------------------------
# EVENT MODEL
# ------------------------------------------------

@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------
# EVENT BUS
# ------------------------------------------------

class EventBus:
    """
    High-performance async event bus.
    Safe for long-running trading bots.
    """

    def __init__(self, maxsize: int = 10_000) -> None:

        self._ingress: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)

        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)

        self._all_subscribers: list[asyncio.Queue[Event]] = []

        self._dispatcher_task: asyncio.Task | None = None

        self._running = asyncio.Event()

    # ------------------------------------------------
    # START
    # ------------------------------------------------

    async def start(self) -> None:

        if self._dispatcher_task and not self._dispatcher_task.done():
            return

        self._running.set()

        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(),
            name="eventbus-dispatcher",
        )

    # ------------------------------------------------
    # STOP
    # ------------------------------------------------

    async def stop(self) -> None:

        self._running.clear()

        if self._dispatcher_task:

            self._dispatcher_task.cancel()

            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------
    # PUBLISH
    # ------------------------------------------------

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:

        event = Event(type=event_type, payload=payload or {})

        try:
            self._ingress.put_nowait(event)

        except asyncio.QueueFull:
            # drop oldest event
            try:
                _ = self._ingress.get_nowait()
            except asyncio.QueueEmpty:
                logger.warning("Ingress queue reported full but empty on drop-oldest")

            self._ingress.put_nowait(event)

    # ------------------------------------------------
    # SUBSCRIBE
    # ------------------------------------------------

    def subscribe(
        self,
        event_type: str | None = None,
        maxsize: int = 2000,
    ) -> asyncio.Queue[Event]:

        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)

        if event_type is None:
            self._all_subscribers.append(queue)
        else:
            self._subscribers[event_type].append(queue)

        return queue

    # ------------------------------------------------
    # UNSUBSCRIBE
    # ------------------------------------------------

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:

        for event_type in list(self._subscribers.keys()):

            if queue in self._subscribers[event_type]:
                self._subscribers[event_type].remove(queue)

        if queue in self._all_subscribers:
            self._all_subscribers.remove(queue)

    # ------------------------------------------------
    # ITER EVENTS
    # ------------------------------------------------

    async def iter_events(
        self,
        queue: asyncio.Queue[Event],
    ) -> AsyncIterator[Event]:

        try:

            while True:
                yield await queue.get()

        finally:

            self.unsubscribe(queue)

    # ------------------------------------------------
    # DISPATCH LOOP
    # ------------------------------------------------

    async def _dispatch_loop(self) -> None:

        while self._running.is_set():

            event = await self._ingress.get()

            targets = []

            targets.extend(self._subscribers.get(event.type, []))
            targets.extend(self._all_subscribers)

            for queue in targets:

                try:

                    if queue.full():

                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            logger.debug("Subscriber queue full check raced with consumer")

                    queue.put_nowait(event)

                except Exception:
                    logger.exception("Failed dispatching event type=%s", event.type)
