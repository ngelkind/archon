"""Confirmation gate: pending_actions + inline keyboards (calibot port).

Any sensitive action (send a message, create an event from inbound content,
dangerous setting change) can be routed through here: a row is written to
pending_actions and the owner is asked. Ownership is enforced (only the owner's
callback is accepted), rows expire, and every decision is audited.

Three transport-neutral seams, so the phone and the Telegram bot are equal peers:

* :func:`resolve_action` — the single-use decision path (claim + executor).
  The claim is one atomic UPDATE, so whichever channel decides first wins and
  every other channel is told "already handled".
* **Notifier registry** — :func:`request_confirmation` writes the row, then asks
  each registered notifier to tell the owner. The Telegram keyboard is the
  built-in notifier; push (Part C) registers alongside it.
* ``rt.events`` — ``approval.pending`` / ``approval.resolved`` for /stream.

Action executors are registered by the modules that own them:
    confirm.register_executor("event.create", fn)
so this module stays import-cycle-free.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ..db import repo
from ..db.tenancy import OWNER_TENANT_ID
from ..runtime import Runtime

Executor = Callable[[Runtime, dict[str, Any]], Awaitable[str]]
_EXECUTORS: dict[str, Executor] = {}

# (rt, action_id, kind, description, payload) -> None
Notifier = Callable[[Runtime, int, str, str, dict[str, Any]], Awaitable[None]]
_NOTIFIERS: list[Notifier] = []

_TTL_MINUTES = 60


def register_executor(kind: str, fn: Executor) -> None:
    _EXECUTORS[kind] = fn


def register_notifier(fn: Notifier) -> None:
    """Add a channel that tells the owner about a new pending action. The
    Telegram keyboard is built in; push adds itself here."""
    _NOTIFIERS.append(fn)


@dataclass(slots=True)
class Outcome:
    """Result of a decision. ``status`` is 'approved' | 'rejected' | 'expired' |
    'already' | 'unknown'. ``ok`` is False when the action was claimed but its
    executor raised."""

    status: str
    detail: str = ""
    ok: bool = True


async def _telegram_notifier(
    rt: Runtime, action_id: int, kind: str, description: str, payload: dict[str, Any]
) -> None:
    bot: Bot | None = rt.send_bot()  # type: ignore[assignment]
    if bot is None:
        rt.audit.note("confirm_no_control_bot", action_id=action_id)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"pa:{action_id}:ok"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"pa:{action_id}:no"),
    ]])
    sent = await bot.send_message(
        rt.settings.telegram_owner_id,
        f"<b>Confirm: {html.escape(kind)}</b>\n{html.escape(description)}",
        reply_markup=keyboard,
    )
    repo.pending_action_set_owner_msg(rt.db, action_id, sent.message_id)


_NOTIFIERS.append(_telegram_notifier)


async def request_confirmation(
    rt: Runtime, *, kind: str, payload: dict[str, Any], description: str,
    chat_pk: int | None = None, tenant: Any = None,
) -> int:
    """Create a pending action and ask the owner. Returns the action id.

    ``tenant`` (a ``TenantContext``) scopes the row; omitted means the
    single-user owner, which is what every existing caller gets."""
    expires = (datetime.now(UTC) + timedelta(minutes=_TTL_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    store = tenant.scope if tenant is not None else rt.db
    action_id = repo.pending_action_create(
        store, kind=kind, payload_json=json.dumps(payload, ensure_ascii=False),
        chat_pk=chat_pk, expires_at=expires,
    )
    rt.events.publish("approval.pending", action_id=action_id, action_kind=kind,
                      description=description, chat_pk=chat_pk,
                      tenant_id=getattr(tenant, "tenant_id", OWNER_TENANT_ID))
    for notifier in tuple(_NOTIFIERS):
        try:
            await notifier(rt, action_id, kind, description, payload)
        except Exception as exc:  # noqa: BLE001 — one bad channel must not lose the action
            rt.audit.note("confirm_notifier_failed", action_id=action_id,
                          error=repr(exc)[:200])
    rt.audit.note("confirm_requested", action_id=action_id, kind=kind)
    return action_id


async def _execute(rt: Runtime, kind: str, payload: dict[str, Any]) -> str:
    executor = _EXECUTORS.get(kind)
    if executor is None:
        return f"no executor registered for {kind}"
    return await executor(rt, payload)


async def resolve_action(
    rt: Runtime, action_id: int, verdict: str, *, actor: str, tenant: Any = None
) -> Outcome:
    """Approve (``verdict == 'ok'``) or reject a pending action exactly once.

    Safe under concurrency from Telegram, the app, and a push action: the claim
    is a single atomic UPDATE, so only one caller ever runs the executor.
    """
    store = tenant.scope if tenant is not None else rt.db
    row = repo.pending_action_get(store, action_id)
    if row is None:
        return Outcome("unknown", "Already handled or unknown.", ok=False)
    if row["status"] != "pending":
        return Outcome("already", "Already handled or unknown.", ok=False)
    if row["expires_at"] < datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"):
        repo.pending_action_expire(store, action_id)
        rt.events.publish("approval.resolved", action_id=action_id,
                          action_kind=row["kind"], status="expired", actor=actor)
        return Outcome("expired", "Expired.", ok=False)

    approved = verdict == "ok"
    if not repo.pending_action_claim(
        store, action_id, "approved" if approved else "rejected"
    ):
        return Outcome("already", "Already handled or unknown.", ok=False)

    if not approved:
        rt.audit.note("confirm_rejected", action_id=action_id, kind=row["kind"],
                      actor=actor)
        rt.events.publish("approval.resolved", action_id=action_id,
                          action_kind=row["kind"], status="rejected", actor=actor)
        return Outcome("rejected", "Rejected")

    rt.audit.note("confirm_approved", action_id=action_id, kind=row["kind"], actor=actor)
    try:
        result = await _execute(rt, row["kind"], json.loads(row["payload_json"]))
        outcome = Outcome("approved", result)
    except Exception as exc:  # noqa: BLE001 — a failed executor must still close the action
        rt.audit.note("confirm_execute_failed", action_id=action_id,
                      error=repr(exc)[:300])
        outcome = Outcome("approved", f"{type(exc).__name__}: {exc}", ok=False)
    rt.events.publish("approval.resolved", action_id=action_id,
                      action_kind=row["kind"], status="approved", ok=outcome.ok,
                      actor=actor)
    return outcome


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
        # Acknowledge the tap NOW. Telegram invalidates a callback query after
        # ~15s, but the executor (a send/revoke, or a loop busy with big agent
        # calls) can take longer — answering first prevents the "query is too
        # old" crash that previously swallowed the action's feedback.
        try:
            await query.answer()
        except Exception:  # noqa: BLE001
            pass

        outcome = await resolve_action(rt, action_id, verdict, actor="telegram")
        # No second query.answer() here: the early ack above already consumed
        # this callback query, so answering again only logs an error.
        if outcome.status in ("unknown", "already"):
            return
        if outcome.status == "expired":
            if query.message:
                await query.message.edit_text(query.message.html_text + "\n\n⏰ Expired")
            return

        if outcome.status == "rejected":
            text = "❌ Rejected"
        elif outcome.ok:
            text = f"✅ Done: {html.escape(outcome.detail)}"
        else:
            text = f"⚠️ Failed: {html.escape(outcome.detail)}"

        if query.message:
            try:
                await query.message.edit_text(
                    (query.message.html_text or "") + f"\n\n{text}"
                )
            except Exception:  # noqa: BLE001 — edit failures must not break the flow
                pass
