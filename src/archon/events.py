"""Realtime event fan-out.

Deliberately NOT the bus. ``bus.py`` is single-consumer with backpressure — the
pipeline must never lose an inbound message, so publishers wait. This hub is the
opposite: many short-lived subscribers (a phone on ``/stream``) reading a
best-effort feed. Publishing is **synchronous, non-blocking and never raises**,
because the tap points sit in the pipeline and LLM hot paths; a slow or absent
phone must not be able to stall them. A full subscriber queue drops its OLDEST
event — the app cares about recent state and reconciles on reconnect.

Events carry identifiers, not message content: subscribers fetch details over
the authenticated API. That keeps content out of the fan-out buffers (and out of
any future push payload) by construction.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from contextlib import contextmanager
from typing import Any, Iterator

_QUEUE_MAX = 200


class EventHub:
    def __init__(self, maxsize: int = _QUEUE_MAX) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._ids = itertools.count(1)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, kind: str, /, **data: Any) -> dict[str, Any]:
        """Fan an event out to every subscriber. Returns the event (useful in
        tests); callers in hot paths ignore it.

        ``kind`` is positional-only so that a payload field may itself be named
        ``kind`` without colliding with the event's own type.
        """
        event = {"id": next(self._ids), "kind": kind, "ts": time.time(), "data": data}
        for queue in tuple(self._subscribers):
            try:
                if queue.full():
                    try:
                        queue.get_nowait()  # drop-oldest, never block
                    except asyncio.QueueEmpty:  # pragma: no cover — race with a reader
                        pass
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001 — a bad subscriber must not break a tap point
                continue
        return event

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register and return a new subscriber queue. Pair with
        :meth:`unsubscribe` — or prefer :meth:`subscription`, which cannot leak
        a queue if the consumer raises."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Stop delivering to a queue. Idempotent."""
        self._subscribers.discard(queue)

    @contextmanager
    def subscription(self) -> Iterator[asyncio.Queue[dict[str, Any]]]:
        """subscribe()/unsubscribe() bound to a block, so a disconnect or an
        exception can never leave a queue attached (used by /stream)."""
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)
