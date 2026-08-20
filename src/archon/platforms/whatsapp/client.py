"""WhatsApp client (neonize async) — session owner, event source, media fetch.

The session file is the one migrated from your-other-project.
Exactly ONE process may own it; the server allows one live socket per linked
device (a second login triggers StreamReplacedEv fights). Fatal events
(LoggedOutEv / TemporaryBanEv / StreamReplacedEv) disable this subsystem and
alert the owner instead of crash-looping — pattern from wa_helper/bot.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...db import repo
from ...runtime import Runtime
from . import events as wa_events


async def _alert_owner(rt: Runtime, text: str) -> None:
    bot = rt.clients.get("control_bot")
    if bot is not None:
        try:
            await bot.send_message(rt.settings.telegram_owner_id, text)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


async def _download_media_if_wanted(rt: Runtime, client: Any, event: Any, inbound) -> None:
    """Download the first media item when the chat has image recognition on."""
    if not inbound.media or inbound.media[0].kind != "image":
        return
    row = repo.chat_get(rt.db, "wa", inbound.chat_id)
    if not row or not row["image_recognition"] or not row["is_whitelisted"]:
        return
    try:
        data: bytes = await client.download_any(event.Message)
        if not data or len(data) > 8_000_000:
            return
        rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c for c in inbound.msg_id if c.isalnum())[:48] or "img"
        path = rt.settings.media_dir / f"wa-{safe_id}.jpg"
        path.write_bytes(data)
        inbound.media[0].local_path = str(path)
        inbound.media[0].mime = "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("wa_media_download_failed", error=repr(exc)[:200])


async def _capture_view_once(rt: Runtime, client: Any, event: Any, inbound) -> None:
    from ...logging_ import capture

    if not capture.capture_enabled(rt, "wa", inbound.chat_id, inbound.chat_kind):
        return
    kind = inbound.media[0].kind if inbound.media else "document"
    try:
        data: bytes = await client.download_any(event.Message)
        if not data:
            return
        rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in inbound.msg_id if c.isalnum())[:48] or "vo"
        ext = {"image": ".jpg", "video": ".mp4", "audio": ".ogg"}.get(kind, ".bin")
        path = rt.settings.media_dir / f"wa-vo-{safe}{ext}"
        path.write_bytes(data)
        await capture.send_capture(
            rt, platform="wa", chat_id=inbound.chat_id, chat_name=inbound.chat_name,
            sender_name=inbound.sender_name or inbound.sender_id, kind=kind,
            local_path=str(path))
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("wa_capture_failed", error=repr(exc)[:200])


async def run(rt: Runtime) -> None:
    from neonize.aioze.client import NewAClient
    from neonize.events import (
        ConnectedEv,
        LoggedOutEv,
        MessageEv,
        PairStatusEv,
        StreamReplacedEv,
        TemporaryBanEv,
    )

    session = rt.settings.wa_session_path
    if not session.exists():
        rt.health["whatsapp"] = "no session (see deploy/MIGRATION.md step 5)"
        return  # clean return: supervisor will not restart-loop

    client = NewAClient(str(session))
    rt.clients["whatsapp"] = client
    loop = asyncio.get_running_loop()
    fatal = asyncio.Event()

    @client.event(ConnectedEv)
    async def on_connected(_c: Any, _ev: Any) -> None:
        rt.health["whatsapp"] = "connected"
        rt.audit.note("wa_connected")
        try:
            groups = await client.get_joined_groups()
            for g in groups:
                gjid = wa_events.jid_str(getattr(g, "JID", None))
                name = getattr(getattr(g, "GroupName", None), "Name", "") or None
                if gjid:
                    repo.chat_upsert(rt.db, "wa", gjid, name, "group")
            rt.audit.note("wa_groups_synced", count=len(groups))
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("wa_group_sync_failed", error=repr(exc)[:200])

    @client.event(MessageEv)
    async def on_message(_c: Any, event: Any) -> None:
        inbound = wa_events.from_message_event(event)
        if inbound is None:
            return
        # Owner-issued /download in a WhatsApp chat: delete + re-send as owner.
        if inbound.is_from_me and inbound.text:
            from .download_cmd import handle_wa_download, is_wa_download

            url = is_wa_download(inbound.text)
            if url:
                await handle_wa_download(rt, client, inbound, url)
                return
        # One-time (view-once) media capture — independent of the whitelist,
        # and regardless of sender (so your own test sends are captured too).
        if inbound.is_ephemeral_media:
            await _capture_view_once(rt, client, event, inbound)
        await _download_media_if_wanted(rt, client, event, inbound)
        await rt.bus.publish(inbound)

    @client.event(PairStatusEv)
    async def on_pair(_c: Any, ev: Any) -> None:
        rt.audit.note("wa_pair_status", detail=str(ev)[:200])

    @client.event(LoggedOutEv)
    async def on_logged_out(_c: Any, _ev: Any) -> None:
        rt.health["whatsapp"] = "LOGGED OUT — re-pair required"
        rt.audit.note("wa_logged_out")
        await _alert_owner(rt, "⚠️ WhatsApp session logged out. Re-pairing is required.")
        fatal.set()

    @client.event(TemporaryBanEv)
    async def on_ban(_c: Any, ev: Any) -> None:
        rt.health["whatsapp"] = "TEMPORARY BAN"
        rt.audit.note("wa_temporary_ban", detail=str(ev)[:200])
        await _alert_owner(rt, "🚫 WhatsApp reports a temporary ban. WhatsApp is disabled.")
        fatal.set()

    @client.event(StreamReplacedEv)
    async def on_replaced(_c: Any, _ev: Any) -> None:
        rt.health["whatsapp"] = "STREAM REPLACED (another client on this session?)"
        rt.audit.note("wa_stream_replaced")
        await _alert_owner(
            rt, "⚠️ WhatsApp stream replaced — is the old bot still running somewhere? "
                "WhatsApp is disabled here to avoid a login fight."
        )
        fatal.set()

    rt.health["whatsapp"] = "connecting"
    connect_task = asyncio.create_task(client.connect())
    fatal_task = asyncio.create_task(fatal.wait())
    done, _pending = await asyncio.wait(
        {connect_task, fatal_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if fatal.is_set():
        # Intentional shutdown of the subsystem: stop the socket, return cleanly
        # so the supervisor does NOT restart into a ban/replace fight.
        connect_task.cancel()
        return
    # The async client's connect() RETURNS once the connection is established;
    # whatsmeow keeps the socket alive and auto-reconnects internally. A real
    # failure surfaces as an exception on the task.
    if connect_task in done:
        exc = connect_task.exception()
        if exc is not None:
            raise exc
    # Connected and healthy — park here until a fatal event fires, so the
    # supervised task stays alive without re-running connect().
    rt.health["whatsapp"] = "connected"
    await fatal.wait()
    connect_task.cancel()
