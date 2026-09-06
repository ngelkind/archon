"""The one place an aiogram ``Bot`` is constructed.

Every bot (control, notifier, product, sub-bots, the token-validation probe)
comes through :func:`make_bot` so that two things can be applied uniformly:

* ``Settings.telegram_api_base`` — when set, requests go to that base URL
  instead of ``https://api.telegram.org``. The end-to-end harness points it at
  a local fake server that aiogram then polls for real; production leaves it
  empty.
* the network ledger's aiohttp trace config (a later layer), so every Bot API
  call is observable without touching call sites.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer


def make_bot(rt: Any, token: str, *, parse_mode: str | None = "HTML") -> Bot:
    base = (getattr(rt.settings, "telegram_api_base", "") or "").strip()
    session = None
    if base:
        session = AiohttpSession(api=TelegramAPIServer.from_base(base.rstrip("/"), is_local=True))
    default = DefaultBotProperties(parse_mode=parse_mode) if parse_mode else None
    return Bot(token=token, session=session, default=default)
