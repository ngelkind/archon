"""SSE sink: turns owner-turn events into a Server-Sent Events stream.

Implements the ``agent.owner.OwnerReplySink`` protocol structurally. Events are
serialized onto an unbounded ``asyncio.Queue`` (so a sink method never blocks the
agent loop) and drained by :meth:`stream`, which ends after the terminal
``final`` / ``error`` event.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


def _sse(event: str, data: dict[str, Any]) -> str:
    """One SSE frame: a named event plus a single JSON ``data:`` line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class SseSink:
    _DONE = object()

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    async def on_tool_call(self, name: str, args: dict[str, Any], call_id: str) -> None:
        await self._queue.put(_sse("tool_call", {"id": call_id, "name": name, "args": args}))

    async def on_tool_result(self, name: str, call_id: str, result: str) -> None:
        await self._queue.put(
            _sse("tool_result", {"id": call_id, "name": name, "result": result})
        )

    async def on_final(self, text: str) -> None:
        await self._queue.put(_sse("final", {"text": text}))
        await self._queue.put(self._DONE)

    async def on_error(self, exc: Exception) -> None:
        # ProviderError messages are content-free by construction; str(exc) is safe.
        await self._queue.put(_sse("error", {"message": str(exc)}))
        await self._queue.put(self._DONE)

    async def stream(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is self._DONE:
                return
            yield item
