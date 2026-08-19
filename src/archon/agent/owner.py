"""Owner conversation: free text in the control bot goes through the agent
with the FULL toolset (scope 'owner') and a persistent rolling context."""

from __future__ import annotations

import html

from aiogram.types import Message

from ..db import repo
from ..llm.base import ChatMessage, ProviderError
from ..runtime import Runtime
from ..tools.registry import Registry, ToolContext
from .agent import run_agent
from .prompts import OWNER_AGENT_SYSTEM


def _control_chat_pk(rt: Runtime) -> int:
    return repo.chat_upsert(
        rt.db, "tg", str(rt.settings.telegram_owner_id), "Archon control", "private"
    )


async def handle_owner_text(
    rt: Runtime, message: Message, text_override: str | None = None
) -> None:
    from ..llm.router import Router

    router: Router = rt.router  # type: ignore[assignment]
    registry: Registry = rt.registry  # type: ignore[assignment]
    text = text_override if text_override is not None else (message.text or "")
    chat_pk = _control_chat_pk(rt)

    history = [
        ChatMessage(role=r["role"], text=r["content"])  # type: ignore[arg-type]
        for r in repo.context_get(rt.db, chat_pk, None, limit=30)
        if r["role"] in ("user", "assistant") and r["content"]
    ]
    history.append(ChatMessage(role="user", text=text))

    ctx = ToolContext(rt=rt, scope="owner", origin_chat_pk=chat_pk)
    try:
        reply = await run_agent(
            router, registry, ctx,
            system=OWNER_AGENT_SYSTEM,
            messages=history,
            purpose="agent",
            chat_pk=chat_pk,
            max_tokens=4096,
        )
    except ProviderError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return

    repo.context_add(rt.db, chat_pk, None, "user", text)
    repo.context_add(rt.db, chat_pk, None, "assistant", reply)
    repo.context_prune(rt.db, chat_pk, None)

    # Telegram HTML mode: escape, keep it simple. 4096-char message cap.
    for chunk_start in range(0, len(reply), 4000):
        await message.answer(html.escape(reply[chunk_start:chunk_start + 4000]))
