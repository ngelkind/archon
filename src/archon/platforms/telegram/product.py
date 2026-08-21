"""The product bot — a SEPARATE Telegram bot from the owner's control bot.

Product users connect *this* bot as their Business chatbot. Keeping it apart
from the personal control bot matters operationally: a product incident (rate
limits, a ban, a token rotation) cannot take the owner's own assistant down,
and the owner's personal bot never shows up in a stranger's chat list.

It runs only when ``multitenant_enabled`` AND ``product_telegram_bot_token`` is
set, so the single-user deployment starts exactly the processes it always did.

Two jobs:

* **`/start <code>`** — redeem the link code the app issued. This is the step
  that proves the person holds the Telegram account, binding ``tg_user_id`` to
  a tenant so a later Business connection can be routed.
* **Plain DMs from a linked user** — the Bot API front door. A user can talk to
  their assistant directly in the bot chat, which works without Telegram
  Premium or a Business connection and is the fallback when Business is
  unavailable to them.

Both paths hard-refuse unknown senders: a stranger who finds the bot gets an
instruction to link, never an assistant and never anyone else's data.
"""

from __future__ import annotations

import html

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ...db.tenancy import OWNER_TENANT_ID
from ...integrations import telegram as tg_integration
from ...runtime import Runtime

_LINK_HELP = (
    "Open the Archon app, go to Settings → Telegram, and tap <b>Link Telegram</b>. "
    "It gives you a code — send it here as <code>/start CODE</code>."
)


def register(dp: Dispatcher, rt: Runtime) -> None:
    """Attach the product handlers. This dispatcher serves ONLY the product bot,
    so every update here belongs to a product user by construction."""

    @dp.message(Command("start"))
    async def cmd_start(message: Message, command: CommandObject) -> None:
        user = message.from_user
        code = (command.args or "").strip()
        if not code:
            await message.answer(f"👋 Welcome to Archon.\n\n{_LINK_HELP}")
            return
        try:
            tenant_id = tg_integration.complete_link(
                rt, code=code, tg_user_id=str(user.id),
                tg_username=user.username, tg_name=user.full_name,
            )
        except tg_integration.TelegramLinkError as exc:
            await message.answer(f"⚠️ {html.escape(str(exc))}\n\n{_LINK_HELP}")
            return
        if tenant_id == OWNER_TENANT_ID:
            # Guard against a code minted for tenant 1 being redeemed from a
            # different Telegram account, which would hand the owner's data to
            # whoever holds that account.
            rt.audit.note("telegram_link_owner_tenant_refused",
                          tg_user_id=str(user.id))
            await message.answer("⚠️ That code can't be used from this account.")
            return
        await message.answer(
            "✅ Telegram linked.\n\nNow connect Archon as your Business chatbot: "
            "<b>Settings → Telegram Business → Chatbots</b>, and choose this bot. "
            "Archon will then help with your private chats.\n\n"
            "You can also just talk to me here."
        )

    @dp.message(F.text & ~F.text.startswith("/"))
    async def product_text(message: Message) -> None:
        """A linked user talking to their assistant in the bot chat."""
        from ...agent.owner import OwnerReplySink, run_owner_turn
        from ...tenant import tenant_context

        user = message.from_user
        tenant_id = tg_integration.tenant_for_user(rt, user.id)
        if tenant_id is None or tenant_id == OWNER_TENANT_ID:
            await message.answer(f"I don't know you yet.\n\n{_LINK_HELP}")
            return

        class _Sink(OwnerReplySink):
            async def on_tool_call(self, name, args, call_id) -> None:
                return None

            async def on_tool_result(self, name, call_id, result) -> None:
                return None

            async def on_final(self, text: str) -> None:
                for start in range(0, len(text), 4000):
                    await message.answer(html.escape(text[start:start + 4000]))

            async def on_error(self, exc: Exception) -> None:
                await message.answer(f"⚠️ {html.escape(str(exc))}")

        await run_owner_turn(rt, message.text or "", _Sink(),
                             tenant=tenant_context(rt, tenant_id))


def build(rt: Runtime) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=rt.settings.product_telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    register(dp, rt)
    # Product users' Business updates arrive on THIS bot, so the business
    # handlers live here too — not on the owner's control bot.
    from . import business

    business.register(dp, rt)
    return bot, dp


def enabled(rt: Runtime) -> bool:
    return bool(rt.settings.multitenant_enabled
                and rt.settings.product_telegram_bot_token.strip())


async def run(rt: Runtime) -> None:
    """Supervised polling loop for the product bot."""
    if not enabled(rt):
        rt.health["product_bot"] = "disabled"
        return

    bot, dp = build(rt)
    rt.clients["product_bot"] = bot
    me = await bot.get_me()
    rt.audit.note("product_bot_started", username=me.username)
    # The deep link needs the @username; take it from Telegram rather than
    # trusting config to match the token.
    if not rt.settings.telegram_bot_username and me.username:
        rt.settings.telegram_bot_username = me.username
    rt.health["product_bot"] = f"polling @{me.username}"
    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "business_connection",
                "business_message",
                "edited_business_message",
                "deleted_business_messages",
            ],
        )
    finally:
        rt.health["product_bot"] = "stopped"
