"""Throttled, flood-resilient sends for the log channel and owner alerts.

Telegram flood-limits rapid sends to a chat (deletion cards + captures can
burst). All out-of-band sends go through :func:`throttled_send`, which
serializes them, keeps ~1 message/sec, and honours ``TelegramRetryAfter`` so
cards/captures are never silently dropped under load.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from ..runtime import Runtime

_MIN_INTERVAL = 1.1  # seconds between sends (well under Telegram's flood limit)
_lock = asyncio.Lock()
_last = 0.0


async def throttled_send(
    rt: Runtime, factory: Callable[[Any], Awaitable[Any]], *, retries: int = 4
) -> Any:
    """Run ``factory(bot)`` serialized + rate-limited, retrying on flood/network
    errors. Returns the send result, or None if it ultimately failed."""
    global _last
    bot = rt.send_bot()
    if bot is None:
        return None
    async with _lock:
        for attempt in range(retries):
            delay = _MIN_INTERVAL - (time.monotonic() - _last)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await factory(bot)
                _last = time.monotonic()
                return result
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramNetworkError:
                # Stale/closed connector: refresh the bot handle and retry.
                await asyncio.sleep(1)
                bot = rt.send_bot()
                if bot is None:
                    break
            except Exception as exc:  # noqa: BLE001
                rt.audit.note("throttled_send_failed", error=repr(exc)[:200])
                _last = time.monotonic()
                return None
        _last = time.monotonic()
        rt.audit.note("throttled_send_gaveup")
        return None
