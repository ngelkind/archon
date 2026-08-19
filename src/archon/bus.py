"""In-process event bus.

One asyncio process, one queue. Platform adapters publish ``InboundMessage``s;
the pipeline consumer is the single subscriber. If the process dies, unread
platform events are re-delivered by the platforms themselves (Telegram polling
offset, WhatsApp redelivery, Gmail historyId), so the queue needs no
persistence.
"""

from __future__ import annotations

import asyncio

from .models import InboundMessage


class Bus:
    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=maxsize)

    async def publish(self, msg: InboundMessage) -> None:
        # Backpressure: if the pipeline is 1000 messages behind, adapters wait
        # rather than growing memory without bound.
        await self._queue.put(msg)

    def publish_nowait(self, msg: InboundMessage) -> None:
        """For non-async callbacks (neonize's Go callback thread via
        run_coroutine_threadsafe is preferred; this is the last resort)."""
        self._queue.put_nowait(msg)

    async def get(self) -> InboundMessage:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def depth(self) -> int:
        return self._queue.qsize()
