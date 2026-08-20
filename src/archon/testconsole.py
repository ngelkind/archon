"""Remote test console — lets the operator run live-client tests over SSH.

The app watches ``data/cmd.trigger``; write a line into it (the 'which' string
for the self-test, e.g. "all" or "wa_video tg_text") and the app runs those
steps against the REAL connected clients, writing the report to
``data/cmd.result``. This avoids a second Telegram session (which would risk
AUTH_KEY_DUPLICATED) and needs no action from the owner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .runtime import Runtime


async def watch(rt: Runtime) -> None:
    trigger = rt.settings.archon_data / "cmd.trigger"
    result = rt.settings.archon_data / "cmd.result"
    rt.health["testconsole"] = "watching"
    while True:
        try:
            if trigger.exists():
                which = trigger.read_text(encoding="utf-8").strip() or "all"
                trigger.unlink(missing_ok=True)
                result.write_text("running…", encoding="utf-8")
                from .selftest import run_selftest
                try:
                    report = await run_selftest(rt, which)
                except Exception as exc:  # noqa: BLE001
                    report = f"crashed: {type(exc).__name__}: {exc}"
                result.write_text(report, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("testconsole_error", error=repr(exc)[:200])
        await asyncio.sleep(3)
