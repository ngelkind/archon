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
        return name, False, f"{type(exc).__name__}: {exc}"


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


_STEPS: dict[str, Any] = {
    "wa_text": _wa_text,
    "wa_video": _wa_video,
    "tg_text": _tg_text,
    "tg_video": _tg_video,
    "tg_schedule": _tg_native_schedule,
}


async def run_selftest(rt: Runtime, which: str = "all") -> str:
    started = time.time()
    rt.audit.note("selftest_start", which=which)
    names = list(_STEPS) if which in ("", "all") else [w for w in which.split() if w in _STEPS]
    results = []
    for name in names:
        results.append(await _step(rt, name, _STEPS[name](rt)))
    lines = [f"{'✅' if ok else '❌'} {name}: {detail}" for name, ok, detail in results]
    passed = sum(1 for _, ok, _ in results)
    header = f"Self-test {passed}/{len(results)} passed ({time.time()-started:.0f}s)"
    rt.audit.note("selftest_done", passed=passed, total=len(results))
    return header + "\n\n" + "\n".join(lines)
