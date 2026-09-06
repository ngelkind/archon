"""Control bot: the single management UI for Archon.

Owner-only aiogram bot on long polling (no inbound ports). Everything the
user can configure is configured here; there is no web UI by design.

Non-owner messages are ignored (and audited) — the control bot is not a
public bot. Business-connection updates are handled in business.py but ride
the same dispatcher so we keep a single polling loop per token.
"""

from __future__ import annotations

import html
import traceback

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ErrorEvent, Message

from ...db import repo
from ...runtime import Runtime
from .botfactory import make_bot

_PAIR_TTL_MINUTES = 10


def build(rt: Runtime) -> tuple[Bot, Dispatcher]:
    bot = make_bot(rt, rt.settings.telegram_bot_token)
    dp = Dispatcher()
    owner_id = rt.settings.telegram_owner_id

    # --- the owner gate, once, for every message and button tap -------------
    # The control bot is not a public bot. One outer middleware replaces the
    # nine copy-pasted `if not is_owner(message): return` checks (only two of
    # which audited the drop) and the callback handler's own check.
    @dp.message.outer_middleware()
    async def owner_only_messages(handler, event: Message, data):
        sender = event.from_user.id if event.from_user else None
        if sender != owner_id:
            rt.audit.note("non_owner_message", sender=sender)
            return None
        return await handler(event, data)

    @dp.callback_query.outer_middleware()
    async def owner_only_callbacks(handler, event: CallbackQuery, data):
        sender = event.from_user.id if event.from_user else None
        if sender != owner_id:
            rt.audit.note("non_owner_callback", sender=sender)
            try:
                await event.answer("Not yours.", show_alert=True)
            except Exception:  # noqa: BLE001 — a stranger's tap is not worth a crash
                pass
            return None
        return await handler(event, data)

    # --- failures reach the owner, not only stderr ---------------------------
    # aiogram catches every handler exception and logs it to the `aiogram.event`
    # logger; with no error observer the owner saw a command do nothing at all.
    @dp.errors()
    async def on_handler_error(event: ErrorEvent) -> None:
        exc = event.exception
        rt.audit.note("handler_error", error=repr(exc)[:300],
                      trace=traceback.format_exc()[-1500:],
                      update=type(event.update.event).__name__
                      if getattr(event.update, "event", None) else "update")
        rt.health["control_bot"] = f"degraded: last handler error {type(exc).__name__}"
        message = getattr(event.update, "message", None)
        if message is None and getattr(event.update, "callback_query", None) is not None:
            message = event.update.callback_query.message
        if message is not None:
            try:
                await message.answer(
                    f"⚠️ That failed: {html.escape(type(exc).__name__)}: "
                    f"{html.escape(str(exc)[:300])}")
            except Exception:  # noqa: BLE001 — the reply itself may be what failed
                pass

    @dp.message(Command("start", "help"))
    async def cmd_help(message: Message) -> None:
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
        day = repo.llm_cost_since(rt.db, "-1 day")
        week = repo.llm_cost_since(rt.db, "-7 days")
        month = repo.llm_cost_since(rt.db, "-30 days")
        if not (day and week and month):
            await message.answer("No LLM cost data yet.")
            return
        await message.answer(
            "<b>LLM costs</b>\n"
            f"24h: ${day['cost']:.4f} ({day['calls']} calls, "
            f"{day['in_tok']}/{day['out_tok']} tok)\n"
            f"7d: ${week['cost']:.4f} ({week['calls']} calls)\n"
            f"30d: ${month['cost']:.4f} ({month['calls']} calls)"
        )

    @dp.message(Command("download"))
    async def cmd_download(message: Message) -> None:
        from . import download_cmd

        url = download_cmd.is_download_command(message.text)
        if not url:
            await message.answer("Usage: /download <video url>")
            return
        await message.answer("⬇️ downloading…")
        await download_cmd.handle_control(rt, message.bot, message.chat.id, url)

    @dp.message(Command("selftest"))
    async def cmd_selftest(message: Message, command: CommandObject) -> None:
        from ...selftest import STEP_NAMES, run_selftest

        which = (command.args or "all").strip()
        unknown = [w for w in which.split() if w not in STEP_NAMES and w != "all"]
        if unknown:
            await message.answer(
                f"Unknown step(s): {html.escape(' '.join(unknown))}. "
                f"Valid: all {html.escape(' '.join(STEP_NAMES))}")
            return
        await message.answer(f"Running self-test ({html.escape(which)})… "
                             "this sends to the test targets.")
        try:
            report = await run_selftest(rt, which)
        except Exception as exc:  # noqa: BLE001
            report = f"self-test crashed: {type(exc).__name__}: {exc}"
        for start in range(0, len(report), 3900):
            await message.answer(html.escape(report[start:start + 3900]))

    @dp.message(Command("pair"))
    async def cmd_pair(message: Message) -> None:
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

    # Any owner text without a command goes to the agent loop.
    @dp.message(F.text & ~F.text.startswith("/"))
    async def owner_text(message: Message) -> None:
        handler = rt.owner_text_handler
        if handler is None:
            await message.answer("Agent loop not wired yet. Use /status.")
        else:
            await handler(message)  # type: ignore[operator]

    # Terminal catch-all, registered LAST (aiogram matches in registration
    # order): a guessed command or a photo/voice/sticker used to fall through
    # every filter and be dropped without a reply or an audit record — the
    # owner typed something and nothing happened.
    @dp.message()
    async def owner_other(message: Message) -> None:
        if message.text and message.text.startswith("/"):
            rt.audit.note("unknown_command", text=message.text[:64])
            await message.answer(
                "Unknown command. Try /help, or just tell me in plain words what you want "
                "(e.g. \"whitelist my chat with Dana\")."
            )
            return
        kind = getattr(message.content_type, "value", message.content_type)
        rt.audit.note("unsupported_owner_message", content_type=str(kind))
        await message.answer(
            "I can read text here (and a Google Contacts .csv export). "
            "Photos, voice notes and stickers are not supported yet."
        )

    return bot, dp


async def run(rt: Runtime, *, handle_signals: bool = True,
              polling_timeout: int = 10) -> None:
    from ...pipeline import confirm
    from . import business

    bot, dp = build(rt)
    confirm.register_handlers(dp, rt)
    business.register(dp, rt)
    # Product users are served by a SEPARATE bot (platforms/telegram/product.py)
    # with its own token and polling loop, so this dispatcher stays the owner's
    # alone — exactly as it was before multi-tenancy.
    rt.clients["control_bot"] = bot
    # A SEPARATE bot instance (same token) dedicated to out-of-band sends
    # (log-channel cards, capture, owner alerts). The polling bot closes its
    # aiohttp session as part of start_polling's lifecycle, which breaks
    # sends made from other tasks ("Connector is closed"); this one is never
    # polled, so its session stays open.
    rt.clients["notifier"] = make_bot(rt, rt.settings.telegram_bot_token)
    rt.health["control_bot"] = "polling"
    me = await bot.get_me()
    rt.audit.note("control_bot_started", username=me.username)
    # Alerts raised before this bot existed (WhatsApp starts first) are
    # queued by alerts.py; deliver them now that there is a channel.
    from ... import alerts

    await alerts.flush_queued(rt)
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
            handle_signals=handle_signals,
            polling_timeout=polling_timeout,
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
