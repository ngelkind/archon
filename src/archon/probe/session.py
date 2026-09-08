"""The test account's own Telethon client, built from PROBE_* settings.

This is a SECOND account (never the owner's) with its OWN api_id/hash, so probe
traffic never rides the owner's api_id and a flag on the test account can't take
the owner down. Built lazily and cached on ``rt.clients['probe_tg']``.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ..runtime import Runtime
from .runner import ProbeError

# TODO(TOS-REVIEW): Telegram — a second user account (test account) automated
# over MTProto to drive live probes — confirm the test account consents and that
# its api_id is distinct from the owner's — review before launch


async def test_client(rt: Runtime) -> Any:
    cached = rt.clients.get("probe_tg")
    if cached is not None:
        return cached
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    s = rt.settings
    session = (s.probe_telethon_session or "").strip()
    if not session:
        raise ProbeError("no PROBE_TELETHON_SESSION configured")
    api_id = s.probe_telegram_api_id or (s.telegram_api_id or 0)
    api_hash = s.probe_telegram_api_hash or (s.telegram_api_hash or "")
    if not api_id or not api_hash:
        raise ProbeError("no PROBE_TELEGRAM_API_ID/HASH (and no owner id to fall back on)")
    client = TelegramClient(StringSession(session), int(api_id), str(api_hash))
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ProbeError("probe Telethon session is not authorized")
    rt.clients["probe_tg"] = client
    return client


async def close_test_client(rt: Runtime) -> None:
    client = rt.clients.pop("probe_tg", None)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.disconnect()
