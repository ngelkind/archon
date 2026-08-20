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

_PAIR_TTL_MINUTES = 10


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
            "/download <i>url</i> — download a video (YouTube/TikTok/…) and send it; "
            "in any of your private chats it re-sends as you\n"
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

    @dp.message(Command("download"))
    async def cmd_download(message: Message) -> None:
        if not is_owner(message):
            return
        from . import download_cmd

        url = download_cmd.is_download_command(message.text)
        if not url:
            await message.answer("Usage: /download <video url>")
            return
        await message.answer("⬇️ downloading…")
        await download_cmd.handle_control(rt, message.bot, message.chat.id, url)

    @dp.message(Command("selftest"))
    async def cmd_selftest(message: Message) -> None:
        if not is_owner(message):
            return
        which = (message.text or "").removeprefix("/selftest").strip() or "all"
        await message.answer(f"Running self-test ({which})… this sends to the test targets.")
        from ...selftest import run_selftest
        try:
            report = await run_selftest(rt, which)
        except Exception as exc:  # noqa: BLE001
            report = f"self-test crashed: {type(exc).__name__}: {exc}"
        await message.answer(html.escape(report))

    @dp.message(Command("pair"))
    async def cmd_pair(message: Message) -> None:
        if not is_owner(message):
            return
        from datetime import UTC, datetime, timedelta

        from ...api.security import hash_secret, mint_pair_code

        code = mint_pair_code()
        expires = (datetime.now(UTC) + timedelta(minutes=_PAIR_TTL_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        repo.api_pair_code_create(
            rt.db,
            code_hash=hash_secret(rt.settings.api_token_pepper, code),
            expires_at=expires,
        )
        rt.audit.note("api_pair_code_issued")
        await message.answer(
            f"<b>Device pairing code:</b> <code>{code}</code>\n"
            f"Enter it in the Archon app within {_PAIR_TTL_MINUTES} minutes to link "
            "this device. One-time use; the code is stored only as a hash."
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

    # Owner sends a Google Contacts .csv → import it into the contact directory.
    @dp.message(F.document)
    async def owner_document(message: Message) -> None:
        if not is_owner(message):
            return
        doc = message.document
        fname = (doc.file_name or "").lower()
        if not (fname.endswith(".csv") or "contact" in fname):
            await message.answer("Send me a Google Contacts <b>.csv</b> export "
                                 "to import your contacts.")
            return
        await message.answer("📇 Importing contacts…")
        try:
            buf = await bot.download(doc)
            text = buf.read().decode("utf-8-sig", errors="replace")
            from ... import contacts as directory

            n = directory.import_csv(rt.db, text)
            row = repo.contact_counts(rt.db)
            await message.answer(
                f"✅ Imported {n} entries. Directory now holds "
                f"{row['entries']} names / {row['unique_numbers']} numbers.")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ Import failed: {type(exc).__name__}: {exc}")

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
    # A SEPARATE bot instance (same token) dedicated to out-of-band sends
    # (log-channel cards, capture, owner alerts). The polling bot closes its
    # aiohttp session as part of start_polling's lifecycle, which breaks
    # sends made from other tasks ("Connector is closed"); this one is never
    # polled, so its session stays open.
    rt.clients["notifier"] = Bot(
        token=rt.settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    rt.health["control_bot"] = "polling"
    me = await bot.get_me()
    rt.audit.note("control_bot_started", username=me.username)
    # Register the command menu so clients (esp. Desktop) show the "/" list.
    from aiogram.types import BotCommand

    try:
        await bot.set_my_commands([
            BotCommand(command="status", description="Subsystem health & uptime"),
            BotCommand(command="ask", description="Ask the agent a question"),
            BotCommand(command="costs", description="LLM spend (day/week/month)"),
            BotCommand(command="download", description="Download a video by URL"),
            BotCommand(command="pair", description="Pair a new control device"),
            BotCommand(command="selftest", description="Run internal self-tests"),
            BotCommand(command="help", description="What Archon can do"),
        ])
    except Exception as exc:  # noqa: BLE001 — a menu failure must not stop the bot
        rt.audit.note("set_my_commands_failed", error=repr(exc)[:120])
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
