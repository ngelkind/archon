"""Sub-bots: extra Telegram bots, each scoped to ONE platform's tools.

Bots cannot be created programmatically (BotFather is manual), so the flow is:
owner creates a bot in BotFather → pastes the token to the main bot
(subbot_register tool) → this manager starts polling it. Each sub-bot talks
to the same agent but sees only its platform's tool subset; it has the same
owner-only access rule as the control bot.

The manager reconciles against the sub_bots table every 30s, so enable/
disable/remove take effect without a restart.
"""

from __future__ import annotations

import asyncio
import html

from aiogram import Dispatcher, F
from aiogram.types import Message

from ...db import repo
from ...runtime import Runtime
from .botfactory import make_bot
from ...tools.registry import Registry, Scope, ToolContext

# Tools visible per platform scope (prefix match), plus a common set.
_COMMON = ("chat_list", "chat_find", "help_tools", "current_datetime",
           "schedule_list", "schedule_cancel", "cost_report")
_PREFIXES = {
    "wa": ("wa_", "schedule_message", "attach_image_from_url", "delay_policy",
           "pending_repl", "persona_", "context_"),
    "tg": ("tg_", "schedule_message", "delay_policy", "pending_repl",
           "persona_", "context_"),
    "gmail": ("email_", "schedule_message"),
}


class FilteredRegistry:
    """Registry view restricted to one platform's tools."""

    def __init__(self, inner: Registry, platform_scope: str) -> None:
        self._inner = inner
        self._allowed_prefixes = _PREFIXES.get(platform_scope, ()) + _COMMON

    def _visible(self, name: str) -> bool:
        return name.startswith(self._allowed_prefixes)

    def specs_for(self, scope: Scope):
        return [s for s in self._inner.specs_for(scope) if self._visible(s.name)]

    async def dispatch(self, ctx: ToolContext, name: str, args: dict) -> str:
        if not self._visible(name):
            return '{"error": "tool not available in this sub-bot"}'
        return await self._inner.dispatch(ctx, name, args)


async def _run_subbot(rt: Runtime, row_id: int, token: str, username: str,
                      platform_scope: str) -> None:
    from ...agent.agent import run_agent
    from ...llm.base import ProviderError
    from ...llm.router import Router

    bot = make_bot(rt, token)
    dp = Dispatcher()
    owner_id = rt.settings.telegram_owner_id
    chat_pk = repo.chat_upsert(rt.db, "tg", f"subbot:{username}",
                               f"Sub-bot @{username}", "private")

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        if not message.from_user or message.from_user.id != owner_id:
            return
        router: Router = rt.router  # type: ignore[assignment]
        registry = FilteredRegistry(rt.registry, platform_scope)  # type: ignore[arg-type]
        ctx = ToolContext(rt=rt, scope="owner", origin_chat_pk=chat_pk)
        from ...agent.prompts import OWNER_AGENT_SYSTEM

        from ...llm.base import ChatMessage

        system = (OWNER_AGENT_SYSTEM
                  + f"\nThis is a dedicated sub-bot scoped to the "
                    f"'{platform_scope}' platform only.")
        history = [
            ChatMessage(role=r["role"], text=r["content"])  # type: ignore[arg-type]
            for r in repo.context_get(rt.db, chat_pk, None, limit=20)
            if r["role"] in ("user", "assistant") and r["content"]
        ]
        history.append(ChatMessage(role="user", text=message.text or ""))
        try:
            reply = await run_agent(router, registry, ctx, system=system,  # type: ignore[arg-type]
                                    messages=history, chat_pk=chat_pk)
        except ProviderError as exc:
            await message.answer(f"⚠️ {html.escape(str(exc))}")
            return
        repo.context_add(rt.db, chat_pk, None, "user", message.text or "")
        repo.context_add(rt.db, chat_pk, None, "assistant", reply)
        repo.context_prune(rt.db, chat_pk, None)
        for i in range(0, len(reply), 4000):
            await message.answer(html.escape(reply[i:i + 4000]))

    rt.audit.note("subbot_started", username=username, scope=platform_scope)
    try:
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        rt.audit.note("subbot_stopped", username=username)


async def run(rt: Runtime) -> None:
    """Reconciliation loop: keep one polling task per enabled sub-bot."""
    running: dict[int, asyncio.Task] = {}
    rt.health["subbots"] = "0 running"
    while True:
        try:
            # Process-wide supervisor: every tenant's sub-bots.
            rows = repo.sub_bot_list_all_tenants(rt.db)
            wanted = {r["id"]: r for r in rows if r["enabled"]}
            for bot_id, task in list(running.items()):
                if bot_id not in wanted or task.done():
                    task.cancel()
                    running.pop(bot_id, None)
            for bot_id, r in wanted.items():
                if bot_id not in running:
                    running[bot_id] = asyncio.create_task(
                        _run_subbot(rt, bot_id, r["token"], r["bot_username"],
                                    r["platform_scope"])
                    )
            rt.health["subbots"] = f"{len(running)} running"
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("subbot_manager_error", error=repr(exc)[:200])
        await asyncio.sleep(30)
