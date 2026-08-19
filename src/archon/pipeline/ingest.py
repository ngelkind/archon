"""Ingest pipeline: normalize → cache → gate → debounce → triage → agent.

Single consumer of the bus. The gate is one pure, fail-closed choke point
(pattern from wa_helper/gate.py) and every decision is audited — "only
whitelisted chats ever reach the LLM" stays checkable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ..agent.agent import run_agent
from ..agent.prompts import INBOUND_AGENT_SYSTEM
from ..agent.triage import triage
from ..db import repo
from ..llm.base import ChatMessage, ProviderError, wrap_untrusted
from ..models import InboundMessage
from ..runtime import Runtime
from ..tools.registry import Registry, ToolContext

_MAX_AGE = timedelta(hours=6)
_DEBOUNCE_S = {"gmail": 5.0, "wa": 20.0, "tg": 20.0}


def decide(rt: Runtime, chat_row, msg: InboundMessage) -> tuple[bool, str]:
    """Pure gate. Returns (allowed, reason). Fail closed."""
    if msg.is_from_me:
        return False, "from_me"
    if msg.is_edit or msg.is_delete:
        return False, "edit_or_delete_event"
    if not (msg.text or "").strip():
        return False, "no_text"
    now = datetime.now(UTC)
    ts = msg.ts if msg.ts.tzinfo else msg.ts.replace(tzinfo=UTC)
    if now - ts > _MAX_AGE:
        return False, "stale_message"
    if msg.platform == "gmail":
        if repo.setting_get(rt.db, "gmail.triage_enabled", True):
            return True, "gmail_triage_enabled"
        return False, "gmail_triage_disabled"
    if chat_row is not None and chat_row["is_whitelisted"]:
        return True, "whitelisted"
    return False, "not_whitelisted"


class _Debouncer:
    """Collect messages per chat; fire once the chat has been quiet briefly."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._buffers: dict[tuple[str, str], list[InboundMessage]] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    def add(self, msg: InboundMessage) -> None:
        key = msg.chat_key
        self._buffers.setdefault(key, []).append(msg)
        if task := self._tasks.get(key):
            task.cancel()
        self._tasks[key] = asyncio.create_task(self._fire_later(key, msg.platform))

    async def _fire_later(self, key: tuple[str, str], platform: str) -> None:
        try:
            await asyncio.sleep(_DEBOUNCE_S.get(platform, 20.0))
        except asyncio.CancelledError:
            return
        batch = self._buffers.pop(key, [])
        self._tasks.pop(key, None)
        if batch:
            try:
                await _process_batch(self.rt, batch)
            except Exception as exc:  # noqa: BLE001
                self.rt.audit.note("pipeline_batch_error", error=repr(exc)[:300])


async def _describe_images(rt: Runtime, router, batch: list[InboundMessage],
                           chat_pk: int | None) -> list[str]:
    """Vision step: downloaded media → short untrusted description."""
    from pathlib import Path

    from ..llm.base import ImagePart

    notes: list[str] = []
    for m in batch:
        for media in m.media:
            if not media.local_path:
                continue
            try:
                data = Path(media.local_path).read_bytes()
                result = await router.complete(
                    purpose="vision",
                    system="You describe images for a personal assistant. Be brief "
                           "(2-4 sentences) and prioritize any event details: dates, "
                           "times, places, names, invitations, tickets, posters.",
                    messages=[ChatMessage(
                        role="user",
                        text="Describe this image.",
                        images=[ImagePart(data=data, mime=media.mime or "image/jpeg")],
                    )],
                    max_tokens=400,
                    chat_pk=chat_pk,
                )
                notes.append(
                    f"[image from {m.sender_name or m.sender_id}] {result.text.strip()}"
                )
            except (ProviderError, OSError) as exc:
                rt.audit.note("vision_failed", error=str(exc)[:200])
    return notes


async def _process_batch(rt: Runtime, batch: list[InboundMessage]) -> None:
    from ..llm.router import Router

    router: Router = rt.router  # type: ignore[assignment]
    registry: Registry = rt.registry  # type: ignore[assignment]
    first = batch[0]
    chat_row = repo.chat_get(rt.db, first.platform, first.chat_id)
    chat_pk = chat_row["id"] if chat_row else None
    auto_reply = bool(chat_row["auto_reply"]) if chat_row else False
    persona_id = chat_row["persona_id"] if chat_row else None

    image_notes = await _describe_images(rt, router, batch, chat_pk)
    combined = "\n---\n".join(
        [f"[{m.sender_name or m.sender_id}] {m.text}" for m in batch if m.text]
        + image_notes
    )
    if not combined.strip():
        return
    verdict = await triage(
        router,
        platform=first.platform,
        chat_name=first.chat_name or first.chat_id,
        sender_name=first.sender_name or first.sender_id,
        text=combined,
        chat_pk=chat_pk,
        auto_reply=auto_reply,
    )
    rt.audit.note("triage", chat=first.chat_id, platform=first.platform,
                  action=verdict.action, confidence=verdict.confidence,
                  reason=verdict.reason)
    if verdict.action == "ignore":
        return

    persona_block = ""
    if persona_id is not None:
        persona = rt.db.query_one("SELECT * FROM personas WHERE id = ?", (persona_id,))
        if persona:
            persona_block = f"\nPersona for replies in this chat:\n{persona['system_prompt']}\n"

    task_lines = []
    if verdict.action in ("calendar", "both"):
        task_lines.append(
            "- If the message(s) contain a concrete event, create it with "
            "calendar_create_event (check calendar_check_conflicts first when a "
            "specific time is given)."
        )
    if verdict.action in ("respond", "both") and auto_reply:
        task_lines.append(
            "- Compose and send an appropriate reply to this chat using the "
            "available send/reply tool for this platform."
        )

    system = INBOUND_AGENT_SYSTEM.format(
        platform=first.platform,
        chat_name=first.chat_name or first.chat_id,
        persona_block=persona_block,
    )
    user_text = (
        f"Today is {datetime.now(UTC).astimezone().isoformat(timespec='minutes')} "
        f"(owner timezone: {rt.settings.timezone}).\n"
        f"New message(s) from {first.sender_name or first.sender_id}:\n"
        f"{wrap_untrusted(combined)}\n\nTasks:\n" + "\n".join(task_lines)
    )

    ctx = ToolContext(
        rt=rt, scope="inbound", origin_chat_pk=chat_pk,
        extras={"source_msg_id": first.msg_id, "platform": first.platform,
                "chat_id": first.chat_id},
    )
    try:
        result = await run_agent(
            router, registry, ctx,
            system=system,
            messages=[ChatMessage(role="user", text=user_text)],
            purpose="agent",
            chat_pk=chat_pk,
        )
        rt.audit.note("inbound_agent_done", chat=first.chat_id,
                      summary=(result or "")[:200])
    except ProviderError as exc:
        rt.audit.note("inbound_agent_failed", chat=first.chat_id, error=str(exc))


async def run(rt: Runtime) -> None:
    """The single bus consumer."""
    debouncer = _Debouncer(rt)
    rt.health["pipeline"] = "running"
    while True:
        msg = await rt.bus.get()
        try:
            chat_pk = repo.chat_upsert(rt.db, msg.platform, msg.chat_id,
                                       msg.chat_name, msg.chat_kind)
            chat_row = repo.chat_get(rt.db, msg.platform, msg.chat_id)

            if msg.is_edit:
                repo.message_mark_edited(rt.db, msg.platform, msg.chat_id,
                                         msg.msg_id, msg.text)
            elif msg.is_delete:
                repo.message_mark_deleted(rt.db, msg.platform, msg.chat_id, msg.msg_id)
            else:
                repo.message_upsert(rt.db, msg, chat_pk)

            allowed, reason = decide(rt, chat_row, msg)
            rt.audit.gate(platform=msg.platform, chat_id=msg.chat_id,
                          sender_id=msg.sender_id, allowed=allowed, reason=reason,
                          text=msg.text)
            if allowed:
                debouncer.add(msg)
        except Exception as exc:  # noqa: BLE001 — consumer must survive anything
            rt.audit.note("pipeline_error", error=repr(exc)[:300])
        finally:
            rt.bus.task_done()
