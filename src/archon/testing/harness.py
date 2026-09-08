"""Boot the real Archon runtime with fake transports and observe what happens.

``Harness.start`` builds a ``Runtime`` through the production wiring
(``app._wire_llm_and_tools``), so every tool module, executor and session
factory the live bot registers is registered here too — the gap that let commit
7214205 ship six broken calendar tools with a green suite was a hand-rolled
test runtime that registered a subset. Subsystems run under the real
supervisor; only the LLM (a :class:`ScriptedProvider`) and, in later layers,
the platform transports are replaced.

Observation is by effect, never by "it did not raise": the audit JSONL (the
source of truth), database rows, and ledgers of what the fakes were asked to
do. Waits poll those at a short interval; nothing in a scenario sleeps for a
fixed time.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import app as app_module
from ..bus import Bus
from ..config import Settings
from ..db import Db, repo
from ..db.migrations import migrate
from ..logging_.audit import AuditLog
from ..models import InboundMessage, MediaRef
from ..pipeline import ingest
from ..runtime import Runtime
from .repo_ledger import Ledger, unwrap_repo, wrap_repo
from .scripted_llm import CHEAP_MODEL, PROVIDER_NAME, STRONG_MODEL, ScriptedProvider

#: What the production pipeline waits before triaging a quiet chat. Pinned by a
#: test so the harness's fast setting can never leak into the deploy.
PRODUCTION_DEBOUNCE_S = {"gmail": 5.0, "wa": 20.0, "tg": 20.0}

_POLL_S = 0.02


class Harness:
    def __init__(self, rt: Runtime, llm: ScriptedProvider, tmp: Path) -> None:
        self.rt = rt
        self.llm = llm
        self.tmp = tmp
        self.tasks: dict[str, asyncio.Task] = {}
        self.telethon: Any = None
        self.neonize: Any = None
        self.bot_api: Any = None
        self.ledger: Ledger | None = None
        self._repo_originals: dict[str, object] | None = None
        self._debounce_backup: dict[str, float] | None = None
        self._audit_offset = 0
        self._stopped = False

    # --- lifecycle ----------------------------------------------------------

    @classmethod
    async def start(
        cls,
        tmp_path: Path,
        *,
        settings: dict[str, Any] | None = None,
        script: ScriptedProvider | None = None,
        subsystems: tuple[str, ...] = ("pipeline",),
        debounce_s: float = 0.02,
        record_repo: bool = False,
        telethon: Any = None,
        neonize: Any = None,
        bot_api: Any = None,
    ) -> Harness:
        data = tmp_path / "data"
        secrets = tmp_path / "secrets"
        overrides = {
            "telegram_bot_token": "8000000001:AAtest-token-for-the-fake-bot-api-server",
            "telegram_owner_id": 1,
            "archon_data": data,
            "archon_secrets": secrets,
            "llm_active_provider": PROVIDER_NAME,
            "tg_log_channel_id": -100777,
            "timezone": "Asia/Jerusalem",
            "api_token_pepper": "test-pepper",
        }
        overrides.update(settings or {})
        if bot_api is True:
            from .fake_bot_api import FakeBotApi

            bot_api = await FakeBotApi().start()
        if bot_api is not None:
            overrides.setdefault("telegram_api_base", bot_api.base_url)
        s = Settings(_env_file=None, **overrides)
        s.ensure_dirs()
        db = Db(s.db_path)
        migrate(db)
        audit = AuditLog(s.audit_log_path, db, store_content=s.store_audit_content)
        rt = Runtime(settings=s, db=db, audit=audit, bus=Bus())
        from ..netlog import NetLedger

        # Same ledger the product boots with, so a scenario can assert on the
        # wire: the offline suite must record nothing but loopback. Alerting is
        # off — an unexpected host in a test is an assertion's job, not a push.
        rt.net = NetLedger(rt, alert=False)
        app_module._wire_llm_and_tools(rt)

        llm = script or ScriptedProvider()
        rt.router._providers[PROVIDER_NAME] = llm  # type: ignore[union-attr]
        repo.setting_set(rt.db, "llm.active_provider", PROVIDER_NAME)
        repo.setting_set(rt.db, f"llm.model.{PROVIDER_NAME}.cheap", CHEAP_MODEL)
        repo.setting_set(rt.db, f"llm.model.{PROVIDER_NAME}.strong", STRONG_MODEL)

        h = cls(rt, llm, tmp_path)
        h.telethon = telethon
        h.neonize = neonize
        h.bot_api = bot_api
        h._debounce_backup = dict(ingest._DEBOUNCE_S)
        for key in ingest._DEBOUNCE_S:
            ingest._DEBOUNCE_S[key] = debounce_s
        if record_repo:
            h.ledger = Ledger()
            h._repo_originals = wrap_repo(h.ledger)
        rt.audit.note("harness_started", subsystems=list(subsystems))
        h._audit_offset = 0
        for name in subsystems:
            h.start_subsystem(name)
        return h

    def start_subsystem(self, name: str, *, backoff_s: float = 0.05) -> asyncio.Task:
        return self.supervise(name, self._subsystem_factory(name), backoff_s=backoff_s)

    def supervise(self, name: str, factory: Callable[[], Awaitable[None]], *,
                  backoff_s: float = 0.05) -> asyncio.Task:
        """Run any coroutine factory under the REAL supervisor (crash loop,
        alerts, deliberate/terminal states), with a short backoff. Registered
        on ``rt.subsystems`` exactly as app.main does, so restart paths work."""
        task = app_module.start_subsystem(self.rt, name, factory, backoff_s=backoff_s)
        self.tasks[name] = task
        return task

    def _subsystem_factory(self, name: str) -> Callable[[], Awaitable[None]]:
        rt = self.rt
        if name == "pipeline":
            return lambda: ingest.run(rt)
        if name == "scheduler":
            from ..scheduler import loop as scheduler_loop

            return lambda: scheduler_loop.run(rt)
        if name == "logworker":
            from ..logging_ import logworker

            return lambda: logworker.run(rt)
        if name == "tg_userbot":
            from ..platforms.telegram import userbot

            if self.telethon is None:
                raise ValueError("start the harness with telethon=FakeTelethonClient(...)")
            return lambda: userbot.run(rt, client=self.telethon)
        if name == "whatsapp":
            from ..platforms.whatsapp import client as wa_client

            if self.neonize is None:
                raise ValueError("start the harness with neonize=FakeAClient(...)")
            return lambda: wa_client.run(rt, client=self.neonize)
        if name == "control_bot":
            from ..platforms.telegram import control

            if self.bot_api is None:
                raise ValueError("start the harness with bot_api=True")
            return lambda: control.run(rt, handle_signals=False, polling_timeout=1)
        raise ValueError(f"harness cannot start subsystem {name!r} yet")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        live = {**self.tasks, **{k: v for k, v in self.rt.tasks.items() if isinstance(v, asyncio.Task)}}
        for task in live.values():
            task.cancel()
        for task in live.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown
                pass
        for key in ("notifier", "control_bot"):
            bot = self.rt.clients.get(key)
            session = getattr(bot, "session", None)
            if session is not None:
                try:
                    await session.close()
                except Exception:  # noqa: BLE001 — teardown
                    pass
        if self.bot_api is not None:
            await self.bot_api.stop()
        if self._debounce_backup is not None:
            ingest._DEBOUNCE_S.update(self._debounce_backup)
        if self._repo_originals is not None:
            unwrap_repo(self._repo_originals)
        if getattr(self.llm, "unscripted", None):
            details = "; ".join(
                f"{r.tier} tools={bool(r.tools)} text={r.last_text[:120]!r}"
                for r in self.llm.unscripted
            )
            raise AssertionError(f"unscripted LLM call(s) during the scenario: {details}")

    async def __aenter__(self) -> Harness:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # --- acting -------------------------------------------------------------

    async def publish(self, msg: InboundMessage) -> None:
        await self.rt.bus.publish(msg)

    async def owner_says(self, text: str, *, wait_reply: bool = True,
                         timeout: float = 5.0) -> str | None:
        """Type ``text`` into the control chat as the owner; return the bot's
        next reply text (or None when ``wait_reply`` is False)."""
        before = len(self.bot_api.calls_of("sendMessage", chat_id=self.rt.settings.telegram_owner_id))
        self.bot_api.owner_message(text, chat_id=self.rt.settings.telegram_owner_id,
                                   from_id=self.rt.settings.telegram_owner_id)
        if not wait_reply:
            return None
        call = await self.bot_api.wait_for_call(
            "sendMessage", chat_id=self.rt.settings.telegram_owner_id,
            count=before + 1, timeout=timeout)
        return call.data.get("text", "")

    def make_message(
        self,
        *,
        platform: str = "tg",
        chat_id: str = "-1001000",
        text: str | None = "hello",
        chat_kind: str = "group",
        source: str | None = None,
        msg_id: str | None = None,
        sender_id: str = "555",
        sender_name: str | None = "Dana",
        chat_name: str | None = None,
        is_from_me: bool = False,
        media: list[MediaRef] | None = None,
        tenant_id: int = 1,
        **extra: Any,
    ) -> InboundMessage:
        if source is None:
            source = {"tg": "userbot" if chat_kind != "private" else "business",
                      "wa": "wa", "gmail": "gmail"}[platform]
        self._msg_counter = getattr(self, "_msg_counter", 0) + 1
        return InboundMessage(
            platform=platform, source=source, chat_id=chat_id, chat_kind=chat_kind,  # type: ignore[arg-type]
            msg_id=msg_id or str(self._msg_counter), sender_id=sender_id,
            sender_name=sender_name, chat_name=chat_name, ts=datetime.now(UTC),
            is_from_me=is_from_me, text=text, media=list(media or []),
            tenant_id=tenant_id, **extra,
        )

    def chat(self, platform: str, chat_id: str, *, name: str | None = None,
             kind: str = "group", whitelisted: bool = False, auto_reply: bool = False,
             log_deletes: bool | None = None, tenant_id: int = 1) -> int:
        """Register a chat row (as the adapters would) and set its policies."""
        from ..db.tenancy import TenantScope

        store = TenantScope(self.rt.db, tenant_id)
        pk = repo.chat_upsert(store, platform, chat_id, name, kind)
        if whitelisted:
            repo.chat_set_field(store, pk, "is_whitelisted", 1)
        if auto_reply:
            repo.chat_set_field(store, pk, "auto_reply", 1)
        if log_deletes is not None:
            repo.chat_set_field(store, pk, "log_deletes", int(log_deletes))
        return pk

    # --- observing ------------------------------------------------------------

    def audit(self, action: str | None = None, **match: Any) -> list[dict[str, Any]]:
        """Audit records written since the harness started, oldest first."""
        path = self.rt.settings.audit_log_path
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        started = False
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not started:
                if rec.get("action") == "harness_started":
                    started = True
                continue
            if action is not None and rec.get("action") != action:
                continue
            if any(rec.get(k) != v for k, v in match.items()):
                continue
            out.append(rec)
        return out

    def gate_rows(self, **match: Any) -> list[dict[str, Any]]:
        return [r for r in self.audit(**match) if r.get("event") == "gate"]

    def tool_rows(self, name: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in self.audit() if r.get("event") == "tool"]
        return [r for r in rows if name is None or r.get("action") == name]

    async def wait_for_audit(self, action: str, *, timeout: float = 5.0,
                             count: int = 1, **match: Any) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            found = self.audit(action, **match)
            if len(found) >= count:
                return found[-1]
            if time.monotonic() > deadline:
                recent = [r.get("action") for r in self.audit()][-12:]
                raise AssertionError(
                    f"no audit record {action!r} matching {match} within {timeout}s; "
                    f"recent actions: {recent}"
                )
            await asyncio.sleep(_POLL_S)

    async def wait_for(self, predicate: Callable[[], Any], *, timeout: float = 5.0,
                       what: str = "condition") -> Any:
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout}s waiting for {what}")
            await asyncio.sleep(_POLL_S)

    def rows(self, sql: str, params: tuple = ()) -> list[Any]:
        return self.rt.db.query(sql, params)

    def row(self, sql: str, params: tuple = ()) -> Any:
        return self.rt.db.query_one(sql, params)

    def assert_non_vacuous(self, *, gate: bool = True, llm: bool = True,
                           writes: bool | None = None) -> None:
        """A scenario that observed no traffic proves nothing — fail it."""
        if gate:
            assert self.gate_rows(), "no gate decision was recorded"
        if llm:
            assert self.llm.requests, "the LLM was never called"
        if writes is None:
            writes = self.ledger is not None
        if writes:
            assert self.ledger is not None and self.ledger.writes(), "no repo write observed"
