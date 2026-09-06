"""Throttled, flood-resilient sends for the log channel and owner alerts.

Telegram flood-limits rapid sends to a chat (deletion cards + captures can
burst). All out-of-band sends go through :func:`throttled_send`, which
serializes them, keeps ~1 message/sec, honours ``TelegramRetryAfter`` and
survives a dead notifier session — so cards, captures and alerts are never
silently dropped under load.

Two things the previous version got wrong, both seen live:

* On ``TelegramNetworkError`` it "refreshed" the bot by calling
  ``rt.send_bot()`` again — which returned the same cached ``Bot`` with the
  same dead aiohttp session, so every retry failed the same way and, once the
  notifier died, every card and alert was lost for the rest of the process
  ("Connector is closed"). The notifier is now actually rebuilt.
* The ``RetryAfter`` sleep ran while holding the send lock, so one flood
  response stalled every other sender behind it (and the ingest loop that
  awaited a card). Waits now happen outside the lock.

State lives on the Runtime, not in module globals, so the harness cannot leak
pacing between scenarios and a second runtime in one process is possible.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from ..runtime import Runtime

_MIN_INTERVAL = 1.1  # seconds between sends (well under Telegram's flood limit)


@dataclass
class SendThrottle:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = 0.0
    consecutive_gaveups: int = 0
    rebuilds: int = 0


def throttle_for(rt: Runtime) -> SendThrottle:
    state = getattr(rt, "send_throttle", None)
    if state is None:
        state = SendThrottle()
        rt.send_throttle = state
    return state


async def rebuild_notifier(rt: Runtime) -> Any:
    """Replace the dedicated notifier bot with a fresh one (new aiohttp
    session). Returns the new bot, or None when no token is configured."""
    from ..platforms.telegram.botfactory import make_bot

    token = rt.settings.telegram_bot_token
    if not token:
        return None
    old = rt.clients.get("notifier")
    if old is not None:
        try:
            await old.session.close()
        except Exception:  # noqa: BLE001 — it is dead anyway
            pass
    bot = make_bot(rt, token)
    rt.clients["notifier"] = bot
    throttle_for(rt).rebuilds += 1
    rt.audit.note("notifier_rebuilt", rebuilds=throttle_for(rt).rebuilds)
    return bot


async def throttled_send(
    rt: Runtime, factory: Callable[[Any], Awaitable[Any]], *, retries: int = 4,
    kind: str = "send",
) -> Any:
    """Run ``factory(bot)`` serialized + rate-limited, retrying on flood and
    network errors. Returns the send result, or None if it ultimately failed
    (audited as ``throttled_send_failed`` / ``throttled_send_gaveup`` with the
    ``kind`` the caller passed, so a burst of losses is diagnosable)."""
    state = throttle_for(rt)
    bot = rt.send_bot()
    if bot is None:
        rt.audit.note("throttled_send_no_bot", kind=kind)
        return None
    last_error: BaseException | None = None
    for _attempt in range(retries):
        wait_s = 0.0
        rebuild = False
        async with state.lock:
            delay = _MIN_INTERVAL - (time.monotonic() - state.last)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await factory(bot)
                state.last = time.monotonic()
                if state.consecutive_gaveups:
                    state.consecutive_gaveups = 0
                    rt.health.pop("notifier", None)
                return result
            except TelegramRetryAfter as exc:
                last_error = exc
                wait_s = float(exc.retry_after) + 1.0
                state.last = time.monotonic()
            except TelegramNetworkError as exc:
                last_error = exc
                rebuild = True
            except Exception as exc:  # noqa: BLE001
                rt.audit.note("throttled_send_failed", kind=kind, error=repr(exc)[:200])
                state.last = time.monotonic()
                return None
        # Sleep and rebuild OUTSIDE the lock so other senders are not stalled.
        if rebuild:
            rt.audit.note("throttled_send_network_error", kind=kind,
                          error=repr(last_error)[:200])
            await asyncio.sleep(0.5)
            bot = await rebuild_notifier(rt) or rt.send_bot()
            if bot is None:
                break
        elif wait_s:
            rt.audit.note("throttled_send_flood", kind=kind, retry_after_s=wait_s)
            await asyncio.sleep(wait_s)
    state.last = time.monotonic()
    state.consecutive_gaveups += 1
    rt.audit.note("throttled_send_gaveup", kind=kind, attempts=retries,
                  last_error=repr(last_error)[:200])
    rt.health["notifier"] = f"send failures ({state.consecutive_gaveups} in a row)"
    return None
