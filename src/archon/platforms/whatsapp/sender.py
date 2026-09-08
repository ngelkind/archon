"""WhatsApp sending with the humanized pattern from wa_helper's _send_slowly:
random pre-delay, typing presence proportional to length, then send.

All sends flow through send_text/send_image; the confirm gate calls these via
the registered executors in tools/whatsapp.py.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from ...db import repo
from ...runtime import Runtime

_TYPING_S_PER_CHAR = 0.045
_TYPING_MAX_S = 10.0


def _delays(rt: Runtime) -> tuple[float, float]:
    lo = float(repo.setting_get(rt.db, "wa.send_delay_min_s", 2.0))
    hi = float(repo.setting_get(rt.db, "wa.send_delay_max_s", 8.0))
    return (min(lo, hi), max(lo, hi))


def _client(rt: Runtime) -> Any:
    client = rt.clients.get("whatsapp")
    if client is None:
        state = rt.health.get("whatsapp", "not running")
        raise RuntimeError(
            f"WhatsApp is not connected (whatsapp: {state}). "
            "If it says LOGGED OUT or NOT PAIRED, send /wa_pair in the control chat.")
    return client


def _to_jid(raw: str) -> Any:
    from neonize.utils import build_jid

    user, _, server = raw.partition("@")
    return build_jid(user, server or "s.whatsapp.net")


# TODO(TOS-REVIEW): WhatsApp — simulates human typing presence proportional to message length before an automated send — review before launch
async def send_text(rt: Runtime, chat_jid: str, text: str, store: Any = None) -> str:
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia

    client = _client(rt)
    target = _to_jid(chat_jid)
    lo, hi = _delays(rt)
    await asyncio.sleep(random.uniform(lo, hi))
    try:
        await client.send_chat_presence(
            target, ChatPresence.CHAT_PRESENCE_COMPOSING,
            ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
        )
        await asyncio.sleep(min(len(text) * _TYPING_S_PER_CHAR, _TYPING_MAX_S))
        await client.send_chat_presence(
            target, ChatPresence.CHAT_PRESENCE_PAUSED,
            ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
        )
    except Exception:  # noqa: BLE001 — presence is cosmetic; the send is what matters
        pass
    resp = await client.send_message(target, text)
    msg_id = getattr(resp, "ID", "") or "sent"
    _cache_outgoing(rt, store, chat_jid, str(msg_id), text)
    return str(msg_id)


def _cache_outgoing(rt: Runtime, store: Any, chat_jid: str, msg_id: str,
                    text: str | None) -> None:
    """Record our own send so deletion cards have a 'before' and wa_get_history
    shows both sides. Goes through the scoped repo accessor: the raw INSERT
    this replaced omitted the NOT NULL tenant_id and was skipped silently."""
    scope = store if store is not None else rt.db
    chat_pk = repo.chat_upsert(scope, "wa", chat_jid, None,
                               "group" if chat_jid.endswith("@g.us") else "private")
    repo.message_cache_outgoing(scope, chat_pk=chat_pk, platform="wa", chat_id=chat_jid,
                                msg_id=msg_id, source="wa", text=text)


async def send_image(rt: Runtime, chat_jid: str, image_path: str,
                     caption: str | None = None, store: Any = None) -> str:
    client = _client(rt)
    target = _to_jid(chat_jid)
    lo, hi = _delays(rt)
    await asyncio.sleep(random.uniform(lo, hi))
    resp = await client.send_image(target, image_path, caption=caption or "")
    msg_id = str(getattr(resp, "ID", "") or "sent")
    _cache_outgoing(rt, store, chat_jid, msg_id, caption or "[image]")
    return msg_id


# TODO(TOS-REVIEW): WhatsApp — controls read receipts programmatically (send/suppress) on an unofficial client — review before launch
async def mark_read(rt: Runtime, chat_jid: str, message_ids: list[str],
                    sender_jid: str | None = None) -> None:
    """Send read receipts. neonize 0.4.3's signature is
    ``mark_read(*ids, chat=, sender=, receipt=)`` — ids as varargs, the
    SENDER of those messages (the chat itself for a DM, the participant for a
    group), and a receipt type. The previous call passed a list positionally
    and no sender, so every wa_mark_read failed."""
    from neonize.utils.enum import ReceiptType

    client = _client(rt)
    if not message_ids:
        return
    await client.mark_read(
        *[str(m) for m in message_ids], chat=_to_jid(chat_jid),
        sender=_to_jid(sender_jid or chat_jid), receipt=ReceiptType.READ,
    )


async def check_number(rt: Runtime, phone: str) -> bool:
    client = _client(rt)
    results = await client.is_on_whatsapp(phone)
    return bool(results and getattr(results[0], "IsIn", False))
