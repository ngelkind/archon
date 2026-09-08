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
from ...tools.settings_ import _find_chat
from .botfactory import make_bot

_PAIR_TTL_MINUTES = 10

#: One source of truth for /help and the Telegram command menu.
_COMMANDS = [
    ("status", "Subsystem health, whitelist counts, spend"),
    ("chats", "List chats and what is monitored/logged"),
    ("whitelist", "Whitelist a chat by name or id (groups need this to be triaged)"),
    ("unwhitelist", "Stop triaging a chat"),
    ("monitor", "monitor all|whitelist [groups] — what gets triaged"),
    ("logall", "logall on|off — edit/delete logging for all groups"),
    ("approvals", "Pending confirmations awaiting your tap"),
    ("ask", "Ask the agent a question"),
    ("costs", "LLM spend (day/week/month)"),
    ("netstat", "Outbound connections and any unexpected hosts"),
    ("download", "Download a video by URL and send it"),
    ("wa_pair", "Re-pair WhatsApp by QR code"),
    ("selftest", "Run internal self-tests"),
    ("livetest", "Run live probes from the test account (observed effects)"),
    ("pair", "Pair a new control device"),
    ("help", "What Archon can do"),
]


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
        lines = ["<b>Archon</b> — your assistant.", ""]
        lines += [f"/{cmd} — {html.escape(desc)}" for cmd, desc in _COMMANDS]
        lines += [
            "",
            ("Or just tell me in plain words — e.g. "
             "\"whitelist my chat with Dana\" or \"what's on my calendar tomorrow\"."),
            "Send a Google Contacts .csv to import your contacts.",
        ]
        await message.answer("\n".join(lines))

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
        net = getattr(rt, "net", None)
        if net is not None:
            summ = net.summary()
            line = f"net: {summ['total']} calls/5m to {len(summ['hosts'])} hosts"
            if summ["unexpected_hosts"]:
                line += f" ⚠ unexpected: {', '.join(summ['unexpected_hosts'][:3])}"
            lines.append(html.escape(line) if not summ["unexpected_hosts"] else line)
        await message.answer("\n".join(lines))

    @dp.message(Command("netstat"))
    async def cmd_netstat(message: Message) -> None:
        net = getattr(rt, "net", None)
        if net is None:
            await message.answer("Network ledger not wired.")
            return
        summ = net.summary()
        lines = [f"<b>Network</b> (last {int(summ['window_s'] // 60)}m)",
                 f"total: {summ['total']} calls"]
        for sub, st in sorted(summ["by_subsystem"].items()):
            err = f", {st['errors']} err" if st["errors"] else ""
            lines.append(f"{html.escape(sub)}: {st['calls']}{err}")
        if summ["unexpected_hosts"]:
            lines.append("⚠ <b>unexpected hosts:</b> "
                         + ", ".join(html.escape(h) for h in summ["unexpected_hosts"]))
        recent = net.recent(limit=12)
        if recent:
            lines.append("<b>recent:</b>")
            for c_ in recent:
                st = c_.error or (str(c_.status) if c_.status is not None else "-")
                dur = f"{c_.duration_ms}ms" if c_.duration_ms is not None else "-"
                lines.append(
                    f"<code>{html.escape(c_.subsystem)}</code> {html.escape(c_.method)} "
                    f"{html.escape(c_.host)}{html.escape(c_.path)} {html.escape(st)} {dur}")
        await message.answer("\n".join(lines[:40]))

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

    def _fmt_chat(r) -> str:
        flags = []
        flags.append("\u2705" if r["is_whitelisted"] else "\u2b1c")
        flags.append("\U0001f4dd" if r["log_deletes"] else "\u2014")
        if r["auto_reply"]:
            flags.append("\U0001f916")
        name = html.escape(r["name"] or r["chat_id"])
        return f"{' '.join(flags)} <code>{html.escape(r['chat_id'])}</code> {name} ({r['kind']})"

    @dp.message(Command("chats"))
    async def cmd_chats(message: Message, command: CommandObject) -> None:
        platform = (command.args or "").strip() or None
        if platform and platform not in ("tg", "wa", "gmail"):
            await message.answer("Usage: /chats [tg|wa|gmail]")
            return
        rows = repo.chat_list(rt.db, platform=platform)
        wl = sum(1 for r in rows if r["is_whitelisted"])
        header = (f"<b>Chats</b> ({len(rows)}, {wl} whitelisted) — "
                  "\u2705 whitelisted \u2b1c not \u00b7 \U0001f4dd logged \u00b7 \U0001f916 auto-reply")
        shown = rows[:40]
        body = "\n".join(_fmt_chat(r) for r in shown) or "(none yet)"
        more = f"\n\n…and {len(rows) - len(shown)} more" if len(rows) > len(shown) else ""
        await message.answer(header + "\n\n" + body + more)

    async def _resolve_one(message: Message, args: str):
        """Return a chat row for a name/id argument, or None after replying with
        guidance (unknown, or ambiguous with the candidates listed)."""
        args = args.strip()
        if not args:
            await message.answer("Usage: /whitelist <chat name or id>")
            return None
        # An exact id on any platform wins.
        for plat in ("tg", "wa", "gmail"):
            row = repo.chat_get(rt.db, plat, args)
            if row is not None:
                return row
        matches = _find_chat(rt.db, None, args)
        if not matches:
            await message.answer(f"No chat matches “{html.escape(args)}”. Try /chats to see them.")
            return None
        if len(matches) > 1 and matches[0]["score"] - matches[1]["score"] < 0.15:
            lines = ["Several chats match — run the command with the exact id:"]
            lines += [f"<code>{html.escape(m['chat_id'])}</code> {html.escape(m['name'] or m['chat_id'])}"
                      for m in matches[:6]]
            await message.answer("\n".join(lines))
            return None
        top = matches[0]
        return repo.chat_get(rt.db, top["platform"], top["chat_id"])

    @dp.message(Command("whitelist"))
    async def cmd_whitelist(message: Message, command: CommandObject) -> None:
        row = await _resolve_one(message, command.args or "")
        if row is None:
            return
        repo.chat_set_field(rt.db, row["id"], "is_whitelisted", 1)
        rt.audit.note("whitelist_add", chat=row["chat_id"], via="command")
        await message.answer(f"\u2705 Whitelisted <b>{html.escape(row['name'] or row['chat_id'])}</b> — "
                             "it will now be triaged.")

    @dp.message(Command("unwhitelist"))
    async def cmd_unwhitelist(message: Message, command: CommandObject) -> None:
        row = await _resolve_one(message, command.args or "")
        if row is None:
            return
        repo.chat_set_field(rt.db, row["id"], "is_whitelisted", 0)
        rt.audit.note("whitelist_remove", chat=row["chat_id"], via="command")
        await message.answer(f"Removed <b>{html.escape(row['name'] or row['chat_id'])}</b> "
                             "from the whitelist.")

    @dp.message(Command("monitor"))
    async def cmd_monitor(message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split()
        if not parts or parts[0] not in ("all", "whitelist"):
            pc = repo.setting_get(rt.db, "monitor.private_chats", rt.settings.monitor_private_chats)
            gr = repo.setting_get(rt.db, "monitor.groups", rt.settings.monitor_groups)
            await message.answer(
                f"Monitoring — private chats: <b>{pc}</b>, groups: <b>{gr}</b>.\n"
                "Usage: /monitor all|whitelist [groups]  (omit 'groups' to set private chats)")
            return
        key = "monitor.groups" if parts[-1] == "groups" else "monitor.private_chats"
        repo.setting_set(rt.db, key, parts[0])
        rt.audit.note("monitor_set", key=key, value=parts[0], via="command")
        await message.answer(f"Set <b>{key}</b> = <b>{parts[0]}</b>.")

    @dp.message(Command("logall"))
    async def cmd_logall(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if arg not in ("on", "off"):
            await message.answer("Usage: /logall on|off")
            return
        want = 1 if arg == "on" else 0
        cur = rt.db.execute(
            "UPDATE chats SET log_deletes = ? WHERE tenant_id = 1 AND kind IN ('group','channel')",
            (want,))
        repo.setting_set(rt.db, "log.groups_default", arg == "on")
        rt.audit.note("logall", enabled=arg == "on", chats=cur.rowcount or 0, via="command")
        await message.answer(f"Edit/delete logging turned <b>{arg}</b> for "
                             f"{cur.rowcount or 0} groups/channels (and for new ones).")

    @dp.message(Command("approvals"))
    async def cmd_approvals(message: Message) -> None:
        rows = rt.db.query(
            "SELECT id, kind, created_at, expires_at FROM pending_actions "
            "WHERE tenant_id = 1 AND status = 'pending' ORDER BY id DESC LIMIT 20")
        if not rows:
            await message.answer("No pending approvals.")
            return
        lines = [f"<b>{len(rows)} pending approval(s)</b>"]
        lines += [f"#{r['id']} {html.escape(r['kind'])} (expires {r['expires_at']})" for r in rows]
        await message.answer("\n".join(lines))

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

    @dp.message(Command("livetest"))
    async def cmd_livetest(message: Message, command: CommandObject) -> None:
        import asyncio

        from ...probe.runner import (
            ProbeDisabled,
            ProbeError,
            render_table,
            run_probes,
        )

        which = (command.args or "all").strip()
        await message.answer(f"🔬 Live probes ({html.escape(which)})… asserting on "
                             "observed effects; this can take a minute.")

        async def _go() -> None:
            try:
                results = await run_probes(rt, which)
                out = render_table(results)
            except ProbeDisabled as exc:
                out = f"Live probes are off: {exc}"
            except ProbeError as exc:
                out = f"Probe error: {exc}"
            except Exception as exc:  # noqa: BLE001
                out = f"Probe run crashed: {type(exc).__name__}: {exc}"
            for start in range(0, len(out), 3900):
                await message.answer(html.escape(out[start:start + 3900]))

        task = asyncio.create_task(_go())
        rt.alert_state.setdefault("_probe_tasks", set()).add(task)
        task.add_done_callback(rt.alert_state["_probe_tasks"].discard)

    @dp.message(Command("wa_pair"))
    async def cmd_wa_pair(message: Message) -> None:
        from aiogram.types import BufferedInputFile

        from ...platforms.whatsapp import pairing

        if pairing.in_progress(rt):
            await message.answer("A WhatsApp pairing attempt is already running.")
            return
        await message.answer(
            "Stopping WhatsApp and requesting a QR code. On your phone: WhatsApp → "
            "Linked devices → Link a device, then scan the code I post. Each code is "
            "valid for about 20 seconds; I will post a fresh one when it rotates."
        )

        async def on_qr(png: bytes, n: int) -> None:
            await message.answer_photo(
                BufferedInputFile(png, filename=f"wa-qr-{n}.png"),
                caption=f"WhatsApp QR #{n} — scan now (valid ~20 s)")

        outcome = await pairing.pair(rt, on_qr=on_qr)
        icon = "✅" if outcome.status in ("paired", "already_paired") else "⚠️"
        await message.answer(f"{icon} WhatsApp pairing: {outcome.status} — "
                             f"{html.escape(outcome.detail)}")

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
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in _COMMANDS])
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
