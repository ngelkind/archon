"""The product bot's front door — handlers for people who are NOT the owner.

Registered only when ``multitenant_enabled``, so the single-user deployment's
dispatcher is untouched: `control.py` keeps ignoring non-owner messages exactly
as before, and these handlers never see the owner's updates.

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

from aiogram import Dispatcher, F
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
    owner_id = rt.settings.telegram_owner_id

    def is_product_user(message: Message) -> bool:
        """Everyone except the owner, whose handlers live in control.py."""
        return bool(message.from_user and message.from_user.id != owner_id)

    @dp.message(Command("start"), F.func(is_product_user))
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

    @dp.message(F.text & ~F.text.startswith("/") & F.func(is_product_user))
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
