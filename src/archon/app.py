"""Application supervisor.

Builds the Runtime, applies DB migrations, then runs every subsystem as a
supervised asyncio task: crash → log → backoff restart → alert the owner
after repeated failures. systemd handles whole-process death; this handles
single-subsystem death without taking the others down.
"""

from __future__ import annotations

import asyncio
import traceback

from .bus import Bus
from .config import load_settings
from .db import Db
from .db.migrations import migrate
from .logging_.audit import AuditLog
from .platforms.telegram import control
from .runtime import Runtime

_MAX_BACKOFF_S = 300


def build_runtime() -> Runtime:
    settings = load_settings()
    if not settings.telegram_bot_token or not settings.telegram_owner_id:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID must be set (see .env.example)."
        )
    db = Db(settings.db_path)
    version = migrate(db)
    audit = AuditLog(settings.audit_log_path, db, store_content=settings.audit_store_content)
    audit.note("startup", schema_version=version)
    rt = Runtime(settings=settings, db=db, audit=audit, bus=Bus())
    _wire_llm_and_tools(rt)
    return rt


def _wire_llm_and_tools(rt: Runtime) -> None:
    from functools import partial

    from .agent.owner import handle_owner_text
    from .llm.router import Router
    from .tools import calendar as calendar_tools
    from .tools import email_ as email_tools
    from .tools import llm_admin, system as system_tools
    from .tools.registry import Registry

    rt.router = Router(rt)
    registry = Registry()
    llm_admin.register(registry)
    system_tools.register(registry)
    calendar_tools.register(registry)
    email_tools.register(registry)
    rt.registry = registry
    rt.owner_text_handler = partial(handle_owner_text, rt)


async def _supervise(rt: Runtime, name: str, coro_factory) -> None:
    backoff = 5
    while True:
        try:
            rt.health[name] = "running"
            await coro_factory()
            # Clean return means intentional shutdown of that subsystem.
            rt.health[name] = "stopped"
            return
        except asyncio.CancelledError:
            rt.health[name] = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor must survive anything
            rt.health[name] = f"crashed: {type(exc).__name__}"
            rt.audit.note(
                "subsystem_crash", subsystem=name, error=repr(exc),
                trace=traceback.format_exc()[-2000:],
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)


async def main() -> None:
    from .pipeline import ingest
    from .platforms.gmail import poller as gmail_poller

    rt = build_runtime()
    tasks = [
        asyncio.create_task(_supervise(rt, "control_bot", lambda: control.run(rt))),
        asyncio.create_task(_supervise(rt, "pipeline", lambda: ingest.run(rt))),
    ]
    if rt.settings.google_token_path.exists():
        tasks.append(
            asyncio.create_task(_supervise(rt, "gmail", lambda: gmail_poller.run(rt)))
        )
    else:
        rt.health["gmail"] = "no token (run scripts/google_consent.py)"
    # M5: whatsapp client — M6: telethon userbot — M8: scheduler
    await asyncio.gather(*tasks)
