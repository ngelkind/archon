"""Owner conversation: free text in the control bot goes through the agent
with the FULL toolset (scope 'owner') and a persistent rolling context.

The transport-neutral core is :func:`run_owner_turn`, which drives the agent
and reports progress/results through an :class:`OwnerReplySink`. Telegram is one
adapter (:class:`TelegramSink`); the API is another (``api.sink.SseSink``). Both
share the same control-chat context, so the app and the Telegram bot are one
rolling memory.
"""

from __future__ import annotations

import html
from typing import Any, Protocol

from aiogram.types import Message

from ..db import repo
from ..llm.base import ChatMessage, ProviderError
from ..runtime import Runtime
from ..tools.registry import Registry, ToolContext
from .agent import AgentEvent, ToolCallEvent, ToolResultEvent, run_agent
from .prompts import OWNER_AGENT_SYSTEM


class OwnerReplySink(Protocol):
    """Where a single owner turn reports what happened. Every method is awaited;
    exactly one of on_final / on_error is called per turn (the terminal event)."""

    async def on_tool_call(self, name: str, args: dict[str, Any], call_id: str) -> None: ...

    async def on_tool_result(self, name: str, call_id: str, result: str) -> None: ...

    async def on_final(self, text: str) -> None: ...

    async def on_error(self, exc: Exception) -> None: ...


def _control_chat_pk(rt: Runtime, tenant: Any = None) -> int:
    """The chat backing this tenant's rolling agent memory.

    Owner (and every legacy caller) gets the same Telegram control chat as
    before, so the personal bot's history is untouched."""
    from ..tenant import owner_context

    return (tenant or owner_context(rt)).control_chat_pk()


async def run_owner_turn(
    rt: Runtime, text: str, sink: OwnerReplySink, *, chat_pk: int | None = None,
    tenant: Any = None,
) -> None:
    """Load context → run the owner agent → save context, reporting via ``sink``.

    Behaviour matches the pre-refactor ``handle_owner_text``: on ``ProviderError``
    the context is NOT saved and only ``on_error`` fires; on success the user and
    assistant turns are persisted, pruned, and ``on_final`` fires. Other
    exceptions propagate to the caller (as before).
    """
    from ..llm.router import Router

    router: Router = rt.router  # type: ignore[assignment]
    registry: Registry = rt.registry  # type: ignore[assignment]
    if tenant is None:
        from ..tenant import owner_context

        tenant = owner_context(rt)
    store = tenant.scope
    if chat_pk is None:
        chat_pk = _control_chat_pk(rt, tenant)

    history = [
        ChatMessage(role=r["role"], text=r["content"])  # type: ignore[arg-type]
        for r in repo.context_get(store, chat_pk, None, limit=30)
        if r["role"] in ("user", "assistant") and r["content"]
    ]
    history.append(ChatMessage(role="user", text=text))

    ctx = ToolContext(rt=rt, scope="owner", origin_chat_pk=chat_pk, tenant=tenant)

    async def _bridge(event: AgentEvent) -> None:
        if isinstance(event, ToolCallEvent):
            await sink.on_tool_call(event.name, event.args, event.call_id)
        elif isinstance(event, ToolResultEvent):
            await sink.on_tool_result(event.name, event.call_id, event.result)

    try:
        reply = await run_agent(
            router, registry, ctx,
            system=OWNER_AGENT_SYSTEM,
            messages=history,
            purpose="agent",
            chat_pk=chat_pk,
            max_tokens=4096,
            on_event=_bridge,
        )
    except ProviderError as exc:
        await sink.on_error(exc)
        return
    except Exception as exc:  # noqa: BLE001 — same terminal-event guarantee as the API sink
        rt.audit.note("owner_turn_failed", error=repr(exc)[:300])
        await sink.on_error(exc)
        return

    repo.context_add(store, chat_pk, None, "user", text)
    if (reply or "").strip():
        # An empty assistant turn is not memory; persisting it would make the
        # next turn look like the model had answered.
        repo.context_add(store, chat_pk, None, "assistant", reply)
    repo.context_prune(store, chat_pk, None)
    await sink.on_final(reply)


class TelegramSink:
    """Owner-reply sink for the control bot. Preserves the exact prior behaviour:
    tool progress is not surfaced; the final reply is HTML-escaped and sent in
    4000-char chunks; a provider error is sent as a ``⚠️`` line."""

    def __init__(self, message: Message) -> None:
        self._message = message

    async def on_tool_call(self, name: str, args: dict[str, Any], call_id: str) -> None:
        # A tool run can take seconds; "typing…" tells the owner the turn is alive.
        try:
            await self._message.bot.send_chat_action(self._message.chat.id, "typing")
        except Exception:  # noqa: BLE001 — cosmetic
            pass

    async def on_tool_result(self, name: str, call_id: str, result: str) -> None:
        return None

    async def on_final(self, text: str) -> None:
        if not (text or "").strip():
            # range(0, 0) sends nothing: the owner's message would get no
            # reply at all while the tools may well have run.
            await self._message.answer("⚠️ The model returned no text for this turn.")
            return
        # Telegram HTML mode: escape, keep it simple. 4096-char message cap.
        for chunk_start in range(0, len(text), 4000):
            await self._message.answer(html.escape(text[chunk_start:chunk_start + 4000]))

    async def on_error(self, exc: Exception) -> None:
        await self._message.answer(f"⚠️ {html.escape(str(exc))}")


async def handle_owner_text(
    rt: Runtime, message: Message, text_override: str | None = None
) -> None:
    text = text_override if text_override is not None else (message.text or "")
    await run_owner_turn(rt, text, TelegramSink(message))
