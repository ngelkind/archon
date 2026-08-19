"""Control bot: the single management UI for Archon.

Owner-only aiogram bot on long polling (no inbound ports). Everything the
user can configure is configured here; there is no web UI by design.

Non-owner messages are ignored (and audited) — the control bot is not a
public bot. Business-connection updates are handled in business.py but ride
the same dispatcher so we keep a single polling loop per token.
"""

from __future__ import annotations

import html

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from ...db import repo
from ...runtime import Runtime


def build(rt: Runtime) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=rt.settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    owner_id = rt.settings.telegram_owner_id

    def is_owner(message: Message) -> bool:
        return message.from_user is not None and message.from_user.id == owner_id

    @dp.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
        if not is_owner(message):
            rt.audit.note("non_owner_message", sender=message.from_user.id if message.from_user else None)
            return
        await message.answer(
            "<b>Archon</b> — your assistant.\n\n"
            "/status — subsystem health, uptime, queue depth\n"
            "/costs — LLM spend today/week/month\n"
            "/ask <i>question</i> — talk to the agent\n"
            "/help — this message"
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not is_owner(message):
            return
        lines = [
            "<b>Archon status</b>",
            f"uptime: {rt.uptime_s() // 3600}h {(rt.uptime_s() % 3600) // 60}m",
            f"queue depth: {rt.bus.depth}",
            f"db: {rt.settings.db_path.name} (schema ok)",
        ]
        for name, state in sorted(rt.health.items()):
            lines.append(f"{html.escape(name)}: {html.escape(state)}")
        active = repo.setting_get(rt.db, "llm.active_provider", rt.settings.llm_active_provider)
        lines.append(f"llm provider: {html.escape(str(active))}")
        await message.answer("\n".join(lines))

    @dp.message(Command("costs"))
    async def cmd_costs(message: Message) -> None:
        if not is_owner(message):
            return
        day = repo.llm_cost_since(rt.db, "-1 day")
        week = repo.llm_cost_since(rt.db, "-7 days")
        month = repo.llm_cost_since(rt.db, "-30 days")
        assert day and week and month
        await message.answer(
            "<b>LLM costs</b>\n"
            f"24h: ${day['cost']:.4f} ({day['calls']} calls, "
            f"{day['in_tok']}/{day['out_tok']} tok)\n"
            f"7d: ${week['cost']:.4f} ({week['calls']} calls)\n"
            f"30d: ${month['cost']:.4f} ({month['calls']} calls)"
        )

    @dp.message(Command("ask"))
    async def cmd_ask(message: Message) -> None:
        if not is_owner(message):
            return
        text = (message.text or "").removeprefix("/ask").strip()
        if not text:
            await message.answer("Usage: /ask <question>")
            return
        handler = rt.owner_text_handler
        if handler is None:
            await message.answer("Agent loop not wired.")
        else:
            await handler(message, text_override=text)  # type: ignore[operator]

    # Catch-all: any owner text without a command goes to the agent loop.
    @dp.message(F.text & ~F.text.startswith("/"))
    async def owner_text(message: Message) -> None:
        if not is_owner(message):
            rt.audit.note("non_owner_message", sender=message.from_user.id if message.from_user else None)
            return
        handler = rt.owner_text_handler
        if handler is None:
            await message.answer("Agent loop not wired yet. Use /status.")
        else:
            await handler(message)  # type: ignore[operator]

    return bot, dp


async def run(rt: Runtime) -> None:
    from ...pipeline import confirm
    from . import business

    bot, dp = build(rt)
    confirm.register_handlers(dp, rt)
    business.register(dp, rt)
    rt.clients["control_bot"] = bot
    rt.health["control_bot"] = "polling"
    me = await bot.get_me()
    rt.audit.note("control_bot_started", username=me.username)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "business_connection",
                "business_message",
                "edited_business_message",
                "deleted_business_messages",
            ],
        )
    finally:
        rt.health["control_bot"] = "stopped"
