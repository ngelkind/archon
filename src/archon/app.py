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

    from .tools import capture as capture_tools
    from .tools import contacts as contacts_tools
    from .tools import contexts as context_tools
    from .tools import logging_ as logging_tools
    from .tools import media as media_tools
    from .tools import scheduling as scheduling_tools
    from .tools import settings_ as settings_tools
    from .tools import subbots as subbot_tools
    from .tools import telegram as telegram_tools
    from .tools import websearch as websearch_tools
    from .tools import whatsapp as whatsapp_tools

    rt.router = Router(rt)
    registry = Registry()
    llm_admin.register(registry)
    system_tools.register(registry)
    calendar_tools.register(registry)
    email_tools.register(registry)
    whatsapp_tools.register(registry)
    telegram_tools.register(registry)
    settings_tools.register(registry)
    logging_tools.register(registry)
    scheduling_tools.register(registry)
    context_tools.register(registry)
    websearch_tools.register(registry)
    subbot_tools.register(registry)
    media_tools.register(registry)
    capture_tools.register(registry)
    contacts_tools.register(registry)
    rt.registry = registry
    rt.owner_text_handler = partial(handle_owner_text, rt)


def _set_health(rt: Runtime, name: str, state: str) -> None:
    """Record a subsystem state, publishing only on an actual change so the
    app's health feed carries transitions, not a heartbeat."""
    if rt.health.get(name) != state:
        rt.health[name] = state
        rt.events.publish("health.change", subsystem=name, state=state)


async def _supervise(rt: Runtime, name: str, coro_factory) -> None:
    backoff = 5
    while True:
        try:
            _set_health(rt, name, "running")
            await coro_factory()
            # Clean return means intentional shutdown of that subsystem.
            _set_health(rt, name, "stopped")
            return
        except asyncio.CancelledError:
            _set_health(rt, name, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor must survive anything
            _set_health(rt, name, f"crashed: {type(exc).__name__}")
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

    if rt.settings.wa_session_path.exists():
        from .platforms.whatsapp import client as wa_client

        tasks.append(
            asyncio.create_task(_supervise(rt, "whatsapp", lambda: wa_client.run(rt)))
        )
    else:
        rt.health["whatsapp"] = "no session (deploy/MIGRATION.md step 5)"

    from .platforms.telegram import subbots as tg_subbots
    from .platforms.telegram import userbot as tg_userbot
    from .scheduler import loop as scheduler_loop

    tasks.append(
        asyncio.create_task(_supervise(rt, "tg_userbot", lambda: tg_userbot.run(rt)))
    )
    tasks.append(
        asyncio.create_task(_supervise(rt, "scheduler", lambda: scheduler_loop.run(rt)))
    )
    tasks.append(
        asyncio.create_task(_supervise(rt, "subbots", lambda: tg_subbots.run(rt)))
    )
    from . import testconsole
    tasks.append(
        asyncio.create_task(_supervise(rt, "testconsole", lambda: testconsole.watch(rt)))
    )

    if rt.settings.api_enabled:
        from .api import server as api_server

        tasks.append(
            asyncio.create_task(_supervise(rt, "api", lambda: api_server.run(rt)))
        )
    else:
        rt.health["api"] = "disabled (set API_ENABLED=true)"
    await asyncio.gather(*tasks)
