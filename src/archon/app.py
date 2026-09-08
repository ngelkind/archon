"""Application supervisor.

Builds the Runtime, applies DB migrations, then runs every subsystem as a
supervised asyncio task: crash → log → backoff restart → alert the owner
after repeated failures. systemd handles whole-process death; this handles
single-subsystem death without taking the others down.
"""

from __future__ import annotations

import asyncio
import time
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
    # The personal bot is meaningless without the owner's Telegram credentials.
    # The product service has no owner and no control bot — it serves signed-up
    # tenants through the API and the product bot — so there they are optional.
    if not settings.multitenant_enabled and (
        not settings.telegram_bot_token or not settings.telegram_owner_id
    ):
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID must be set (see .env.example). "
            "For a product-only deployment set MULTITENANT_ENABLED=true instead."
        )
    db = Db(settings.db_path)
    version = migrate(db)
    audit = AuditLog(settings.audit_log_path, db, store_content=settings.store_audit_content)
    audit.note("startup", schema_version=version)
    rt = Runtime(settings=settings, db=db, audit=audit, bus=Bus())
    from .db.migrations import backfill_monitor_defaults

    backfill_monitor_defaults(rt)
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
    # Teach the session registry how to build a tenant's Google clients.
    from .integrations import google as google_integration
    from .integrations import telegram_userbot as tg_userbot_integration
    from .integrations import whatsapp as wa_integration

    google_integration.register(rt)
    wa_integration.register(rt)
    tg_userbot_integration.register(rt)


def _set_health(rt: Runtime, name: str, state: str) -> None:
    """Record a subsystem state, publishing only on an actual change so the
    app's health feed carries transitions, not a heartbeat."""
    if rt.health.get(name) != state:
        rt.health[name] = state
        rt.events.publish("health.change", subsystem=name, state=state)


#: Health strings a subsystem leaves behind when it stops ON PURPOSE. A clean
#: return with one of these is "correctly idle": no restart, no alert.
DELIBERATE_PREFIXES = ("disabled", "no session", "not configured", "no token", "watching")
#: Health strings for a session that is over and must NOT be restarted into
#: (a restart would fight a ban or a replaced stream). The owner is told once.
TERMINAL_PREFIXES = ("LOGGED OUT", "TEMPORARY BAN", "STREAM REPLACED", "SESSION INVALID",
                     "NOT PAIRED")
#: A subsystem that stayed up this long before failing gets its backoff and
#: failure count reset — the failure is fresh, not the same crash loop.
_HEALTHY_S = 600.0
_ALERT_AFTER = 2


def _starts_with_any(state: str, prefixes: tuple[str, ...]) -> bool:
    return any(state.startswith(p) for p in prefixes)


async def _supervise(rt: Runtime, name: str, coro_factory, *, backoff_s: float = 5.0) -> None:
    """Run one subsystem forever: crash -> log -> backoff -> restart, and TELL
    THE OWNER after repeated failures (the second consecutive one, then at most
    hourly per subsystem while it keeps failing).

    A clean return is not automatically fine. If the subsystem left a
    deliberate state ("disabled (...)", "no session (...)") it is idle by
    configuration and stays down quietly. If it left a terminal state ("LOGGED
    OUT", "TEMPORARY BAN") it stays down and the owner is told. Anything else —
    it simply returned while claiming to be running — is treated exactly like
    a crash: audited, restarted with backoff, alerted. That last case is how
    the Telethon userbot used to die for the rest of the process with only a
    health string as evidence.
    """
    from . import alerts

    backoff = backoff_s
    failures = 0

    async def _failed(kind: str, detail: str) -> None:
        nonlocal failures, backoff
        failures += 1
        if failures >= _ALERT_AFTER:
            await alerts.alert_owner(
                rt, f"subsystem:{name}",
                f"⚠️ {name} {kind} {failures}× in a row: {detail[:200]}\n"
                f"/status for details; retrying in {int(backoff)}s.",
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_S)

    while True:
        started = time.monotonic()
        try:
            _set_health(rt, name, "running")
            await coro_factory()
        except asyncio.CancelledError:
            _set_health(rt, name, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor must survive anything
            if time.monotonic() - started > _HEALTHY_S:
                failures, backoff = 0, backoff_s
            _set_health(rt, name, f"crashed: {type(exc).__name__}")
            rt.audit.note(
                "subsystem_crash", subsystem=name, error=repr(exc),
                trace=traceback.format_exc()[-2000:], consecutive=failures + 1,
            )
            await _failed("crashed", f"{type(exc).__name__}: {exc}")
            continue

        state = rt.health.get(name, "")
        if state != "running" and _starts_with_any(state, DELIBERATE_PREFIXES):
            rt.audit.note("subsystem_idle", subsystem=name, state=state)
            return
        if state != "running" and _starts_with_any(state, TERMINAL_PREFIXES):
            rt.audit.note("subsystem_terminal", subsystem=name, state=state)
            await alerts.alert_owner(
                rt, f"subsystem:{name}:terminal",
                f"⛔ {name} is down: {state}. It will not restart on its own.", force=True,
            )
            return
        # Returned while claiming to run (or with an unrecognised state): a
        # quiet death. Same treatment as a crash.
        if time.monotonic() - started > _HEALTHY_S:
            failures, backoff = 0, backoff_s
        if state == "running":
            _set_health(rt, name, "stopped unexpectedly")
        rt.audit.note("subsystem_stopped", subsystem=name, state=rt.health.get(name, ""),
                      consecutive=failures + 1)
        await _failed("stopped unexpectedly", rt.health.get(name, ""))


def start_subsystem(rt: Runtime, name: str, coro_factory=None, *,
                    backoff_s: float = 5.0) -> asyncio.Task:
    """Start (or restart) one supervised subsystem.

    Factories are remembered on ``rt.subsystems`` so an owner command can bring
    a subsystem back after a terminal state — WhatsApp after ``/wa_pair`` —
    without a process restart. A still-running task for ``name`` is cancelled
    first.
    """
    if coro_factory is not None:
        rt.subsystems[name] = coro_factory
    factory = rt.subsystems[name]
    old = rt.tasks.get(name)
    if old is not None and not old.done():
        old.cancel()
    task = asyncio.create_task(_supervise(rt, name, factory, backoff_s=backoff_s),
                               name=f"archon:{name}")
    rt.tasks[name] = task
    return task


async def main() -> None:
    from .pipeline import ingest
    from .platforms.gmail import poller as gmail_poller

    rt = build_runtime()
    start_subsystem(rt, "pipeline", lambda: ingest.run(rt))

    if rt.settings.telegram_bot_token and rt.settings.telegram_owner_id:
        start_subsystem(rt, "control_bot", lambda: control.run(rt))
    else:
        # Product-only deployment: there is no owner to control.
        rt.health["control_bot"] = "disabled (no owner Telegram credentials)"

    # The poller serves the owner's token file AND every tenant who linked
    # Google, so in product mode it must run even with no owner token on disk.
    if rt.settings.google_token_path.exists() or rt.settings.multitenant_enabled:
        start_subsystem(rt, "gmail", lambda: gmail_poller.run(rt))
    else:
        rt.health["gmail"] = "no token (run scripts/google_consent.py)"

    # WhatsApp decides for itself (off switch, no session, pairing state) and
    # leaves a deliberate health string in each case.
    from .platforms.whatsapp import client as wa_client

    start_subsystem(rt, "whatsapp", lambda: wa_client.run(rt))

    from .platforms.telegram import subbots as tg_subbots
    from .platforms.telegram import userbot as tg_userbot
    from .scheduler import loop as scheduler_loop

    start_subsystem(rt, "tg_userbot", lambda: tg_userbot.run(rt))
    start_subsystem(rt, "scheduler", lambda: scheduler_loop.run(rt))
    from .logging_ import logworker

    start_subsystem(rt, "logworker", lambda: logworker.run(rt))
    start_subsystem(rt, "subbots", lambda: tg_subbots.run(rt))
    from . import testconsole

    start_subsystem(rt, "testconsole", lambda: testconsole.watch(rt))

    from .platforms.telegram import product as tg_product

    if tg_product.enabled(rt):
        start_subsystem(rt, "product_bot", lambda: tg_product.run(rt))
    else:
        rt.health["product_bot"] = "disabled (multitenant + PRODUCT_TELEGRAM_BOT_TOKEN)"

    if rt.settings.ntfy_base_url.strip():
        # Wakes paired devices for approvals/alerts. Content-free payloads; the
        # notifier is a no-op unless this is configured, so registering is safe.
        from .api import push

        push.register(rt)

    if rt.settings.api_enabled:
        from .api import server as api_server

        start_subsystem(rt, "api", lambda: api_server.run(rt))
    else:
        rt.health["api"] = "disabled (set API_ENABLED=true)"
    # Subsystems restarted later (rt.tasks changes) are picked up because the
    # gather is over the live task set at each iteration.
    while True:
        await asyncio.gather(*list(rt.tasks.values()))
        if all(t.done() for t in rt.tasks.values()):
            return
