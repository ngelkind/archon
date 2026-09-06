"""Live end-to-end self-test, triggered by the owner via /selftest.

Exercises each OUTBOUND feature using the real connected clients and writes a
per-step result to the audit log (so it can be read over SSH) and replies to
the owner with a summary. Inbound features (deletion logging, capture, auto
events) are tested separately by having the owner send/delete real messages.
"""

from __future__ import annotations

import time
import traceback
from typing import Any

from .platforms import downloader
from .runtime import Runtime

WA_TEST = "972555000003@s.whatsapp.net"
TG_TEST = "example_test_account"  # username without @
TIKTOK = "https://www.tiktok.com/@scout2015/video/6718335390845095173"


async def _step(rt: Runtime, name: str, coro) -> tuple[str, bool, str]:
    try:
        detail = await coro
        rt.audit.note("selftest_step", step=name, ok=True, detail=str(detail)[:200])
        return name, True, str(detail)[:200]
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("selftest_step", step=name, ok=False,
                      error=repr(exc)[:300], trace=traceback.format_exc()[-600:])
        return name, False, f"{type(exc).__name__}: {str(exc)[:200]}"


async def _wa_text(rt: Runtime) -> str:
    from .platforms.whatsapp import sender
    mid = await sender.send_text(rt, WA_TEST, "Archon self-test — WhatsApp text ✅")
    return f"sent id={mid}"


async def _wa_video(rt: Runtime) -> str:
    client = rt.clients.get("whatsapp")
    if client is None:
        raise RuntimeError("whatsapp client not connected")
    v = await downloader.download(TIKTOK, rt.settings.media_dir)
    from neonize.utils import build_jid
    user, _, server = WA_TEST.partition("@")
    await client.send_video(build_jid(user, server), v.path,
                            caption="Archon self-test — WhatsApp video ✅")
    return f"downloaded {round(v.size_bytes/1024/1024,1)}MB and sent"


async def _tg_text(rt: Runtime) -> str:
    client = rt.clients.get("tg_userbot")
    if client is None:
        raise RuntimeError("telegram userbot not connected")
    entity = await client.get_entity(TG_TEST)
    msg = await client.send_message(entity, "Archon self-test — Telegram text ✅")
    return f"sent id={getattr(msg, 'id', '?')}"


async def _tg_video(rt: Runtime) -> str:
    client = rt.clients.get("tg_userbot")
    if client is None:
        raise RuntimeError("telegram userbot not connected")
    v = await downloader.download(TIKTOK, rt.settings.media_dir)
    entity = await client.get_entity(TG_TEST)
    await client.send_file(entity, v.path, caption="Archon self-test — Telegram video ✅",
                           supports_streaming=True)
    return f"downloaded {round(v.size_bytes/1024/1024,1)}MB and sent"


async def _tg_native_schedule(rt: Runtime) -> str:
    from datetime import UTC, datetime, timedelta
    client = rt.clients.get("tg_userbot")
    if client is None:
        raise RuntimeError("telegram userbot not connected")
    entity = await client.get_entity(TG_TEST)
    when = datetime.now(UTC) + timedelta(minutes=2)
    await client.send_message(entity, "Archon self-test — TG native schedule (+2min) ✅",
                              schedule=when)
    return "scheduled +2min (native)"


async def _wa_download_cmd(rt: Runtime) -> str:
    """Exercise the exact /download-in-WhatsApp code path (LID convert, revoke,
    re-send), as if the owner typed it in the chat with the test number."""
    from datetime import UTC, datetime
    from .models import InboundMessage
    from .platforms.whatsapp import download_cmd
    client = rt.clients.get("whatsapp")
    if client is None:
        raise RuntimeError("whatsapp client not connected")
    fake = InboundMessage(
        platform="wa", source="wa", chat_id=WA_TEST, chat_kind="private",
        msg_id="SELFTEST", sender_id="972555000001@s.whatsapp.net",
        ts=datetime.now(UTC), is_from_me=True, text=f"/download {TIKTOK}",
    )
    url = download_cmd.is_wa_download(fake.text)
    await download_cmd.handle_wa_download(rt, client, fake, url)
    return "handle_wa_download ran (check audit download_sent/wa_download_send_failed)"


async def _calendar_event(rt: Runtime) -> str:
    from datetime import UTC, datetime, timedelta
    from .calendar_.client import CalendarClient
    from .platforms.google_auth import GoogleAuth
    import asyncio as _a
    cal = rt.clients.get("calendar") or CalendarClient(
        GoogleAuth(rt.settings.google_token_path), rt.settings.timezone)
    rt.clients["calendar"] = cal
    start = (datetime.now() + timedelta(days=1)).replace(hour=15, minute=0, second=0,
                                                         microsecond=0)
    created = await _a.to_thread(
        cal.create_event, calendar_id="primary",
        title="Archon self-test event", start_iso=start.isoformat())
    return f"created event {created['id']} — {created.get('htmlLink','')[:60]}"


async def _gmail_send(rt: Runtime) -> str:
    import asyncio as _a
    from .platforms.gmail.client import GmailClient
    from .platforms.google_auth import GoogleAuth
    gm = rt.clients.get("gmail") or GmailClient(GoogleAuth(rt.settings.google_token_path))
    rt.clients["gmail"] = gm
    addr = await _a.to_thread(gm.get_profile_address)
    mid = await _a.to_thread(gm.send, to=addr, subject="Archon self-test",
                             body="This is an Archon self-test email to yourself.")
    return f"sent email to {addr} id={mid}"


async def _notify_logchannel(rt: Runtime) -> str:
    """Verify the in-process notifier bot can post to the log channel (the
    path that was failing with 'Connector is closed')."""
    from .logging_.capture import _log_channel
    ch = _log_channel(rt)
    if not ch:
        raise RuntimeError("no log channel configured")
    bot = rt.send_bot()
    if bot is None:
        raise RuntimeError("no send bot")
    await bot.send_message(ch, "Archon self-test — in-process log-channel send ✅")
    return f"posted to log channel {ch}"


_STEPS: dict[str, Any] = {
    "notify": _notify_logchannel,
    "wa_text": _wa_text,
    "wa_video": _wa_video,
    "wa_download_cmd": _wa_download_cmd,
    "tg_text": _tg_text,
    "tg_video": _tg_video,
    "tg_schedule": _tg_native_schedule,
    "calendar": _calendar_event,
    "gmail": _gmail_send,
}


STEP_NAMES = tuple(_STEPS)


async def run_selftest(rt: Runtime, which: str = "all") -> str:
    started = time.time()
    rt.audit.note("selftest_start", which=which)
    words = which.split()
    unknown = [w for w in words if w not in _STEPS and w != "all"]
    if unknown:
        raise ValueError(f"unknown self-test step(s): {' '.join(unknown)}; "
                         f"valid: {' '.join(_STEPS)}")
    names = list(_STEPS) if (not words or "all" in words) else [w for w in words if w in _STEPS]
    results = []
    for name in names:
        results.append(await _step(rt, name, _STEPS[name](rt)))
    lines = [f"{'✅' if ok else '❌'} {name}: {detail}" for name, ok, detail in results]
    passed = sum(1 for _, ok, _ in results)
    header = f"Self-test {passed}/{len(results)} passed ({time.time()-started:.0f}s)"
    rt.audit.note("selftest_done", passed=passed, total=len(results))
    return header + "\n\n" + "\n".join(lines)
