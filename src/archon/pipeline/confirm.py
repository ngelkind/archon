"""Confirmation gate: pending_actions + inline keyboards (calibot port).

Any sensitive action (send a message, create an event from inbound content,
dangerous setting change) can be routed through here: a row is written to
pending_actions and the owner gets an Approve/Reject keyboard on the control
bot. Ownership is enforced (only the owner's callback is accepted), rows
expire, and every decision is audited.

Action executors are registered by the modules that own them:
    confirm.register_executor("event.create", fn)
so this module stays import-cycle-free.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ..db import repo
from ..runtime import Runtime

Executor = Callable[[Runtime, dict[str, Any]], Awaitable[str]]
_EXECUTORS: dict[str, Executor] = {}

_TTL_MINUTES = 60


def register_executor(kind: str, fn: Executor) -> None:
    _EXECUTORS[kind] = fn


async def request_confirmation(
    rt: Runtime, *, kind: str, payload: dict[str, Any], description: str,
    chat_pk: int | None = None,
) -> int:
    """Create a pending action and ask the owner. Returns the action id."""
    expires = (datetime.now(UTC) + timedelta(minutes=_TTL_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    cur = rt.db.execute(
        "INSERT INTO pending_actions (kind, payload_json, chat_pk, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (kind, json.dumps(payload, ensure_ascii=False), chat_pk, expires),
    )
    action_id = int(cur.lastrowid)

    bot: Bot | None = rt.send_bot()  # type: ignore[assignment]
    if bot is None:
        rt.audit.note("confirm_no_control_bot", action_id=action_id)
        return action_id
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"pa:{action_id}:ok"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"pa:{action_id}:no"),
    ]])
    sent = await bot.send_message(
        rt.settings.telegram_owner_id,
        f"<b>Confirm: {html.escape(kind)}</b>\n{html.escape(description)}",
        reply_markup=keyboard,
    )
    rt.db.execute("UPDATE pending_actions SET owner_msg_id = ? WHERE id = ?",
                  (sent.message_id, action_id))
    rt.audit.note("confirm_requested", action_id=action_id, kind=kind)
    return action_id


async def _execute(rt: Runtime, kind: str, payload: dict[str, Any]) -> str:
    executor = _EXECUTORS.get(kind)
    if executor is None:
        return f"no executor registered for {kind}"
    return await executor(rt, payload)


def register_handlers(dp: Dispatcher, rt: Runtime) -> None:
    @dp.callback_query(F.data.startswith("pa:"))
    async def on_confirm(query: CallbackQuery) -> None:
        if query.from_user.id != rt.settings.telegram_owner_id:
            await query.answer("Not yours.", show_alert=True)
            return
        try:
            _, raw_id, verdict = (query.data or "").split(":")
            action_id = int(raw_id)
        except ValueError:
            await query.answer("Malformed callback.")
            return

        # Ownership + single-use enforced in SQL (calibot pattern).
        row = rt.db.query_one(
            "SELECT * FROM pending_actions WHERE id = ? AND status = 'pending'",
            (action_id,),
        )
        if row is None:
            await query.answer("Already handled or unknown.")
            return
        if row["expires_at"] < datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"):
            rt.db.execute("UPDATE pending_actions SET status = 'expired' WHERE id = ?",
                          (action_id,))
            await query.answer("Expired.")
            if query.message:
                await query.message.edit_text(query.message.html_text + "\n\n⏰ Expired")
            return

        if verdict == "ok":
            rt.db.execute("UPDATE pending_actions SET status = 'approved' WHERE id = ?",
                          (action_id,))
            rt.audit.note("confirm_approved", action_id=action_id, kind=row["kind"])
            try:
                result = await _execute(rt, row["kind"], json.loads(row["payload_json"]))
                outcome = f"✅ Done: {html.escape(result)}"
            except Exception as exc:  # noqa: BLE001
                outcome = f"⚠️ Failed: {html.escape(f'{type(exc).__name__}: {exc}')}"
                rt.audit.note("confirm_execute_failed", action_id=action_id,
                              error=repr(exc)[:300])
        else:
            rt.db.execute("UPDATE pending_actions SET status = 'rejected' WHERE id = ?",
                          (action_id,))
            rt.audit.note("confirm_rejected", action_id=action_id, kind=row["kind"])
            outcome = "❌ Rejected"

        await query.answer()
        if query.message:
            try:
                await query.message.edit_text(
                    (query.message.html_text or "") + f"\n\n{outcome}"
                )
            except Exception:  # noqa: BLE001 — edit failures must not break the flow
                pass
