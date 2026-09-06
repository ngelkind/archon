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
from ..db.tenancy import TenantScope
from ..tenant import tenant_context
from ..llm.base import ChatMessage, ProviderError, wrap_untrusted
from ..logging_ import tglog
from ..models import InboundMessage
from ..runtime import Runtime
from ..tools.registry import Registry, ToolContext

_MAX_AGE = timedelta(hours=6)
_DEBOUNCE_S = {"gmail": 5.0, "wa": 20.0, "tg": 20.0}


def decide(rt: Runtime, chat_row, msg: InboundMessage) -> tuple[bool, str]:
    """Pure gate. Returns (allowed, reason). Fail closed.

    Settings are read under the MESSAGE's tenant: one user turning off Gmail
    triage, or nominating a log channel, must not change anyone else's gate."""
    store = TenantScope(rt.db, msg.tenant_id)
    # Never process the log channel (the bot posts there; re-ingesting it loops).
    log_ch = repo.setting_get(store, "log.channel_id", rt.settings.tg_log_channel_id)
    if log_ch is not None and msg.chat_id == str(log_ch):
        return False, "log_channel"
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
        if repo.setting_get(store, "gmail.triage_enabled", True):
            return True, "gmail_triage_enabled"
        return False, "gmail_triage_disabled"
    if chat_row is not None and chat_row["is_whitelisted"]:
        return True, "whitelisted"
    return False, "not_whitelisted"


async def _wa_whitelisted_via_counterpart(rt: Runtime, msg: InboundMessage) -> bool:
    """A WhatsApp contact's LID (…@lid) and phone JID (…@s.whatsapp.net) are the
    same person but SEPARATE chat rows. Incoming messages arrive on the LID, yet
    the owner usually whitelists the phone number. So if a LID chat isn't
    whitelisted directly, resolve its phone JID and honour a whitelist on either;
    then propagate the flag to the LID row so the next check is instant."""
    if msg.platform != "wa" or not msg.chat_id.endswith("@lid"):
        return False
    client = rt.clients.get("whatsapp")
    if client is None:
        return False
    try:
        from neonize.utils import build_jid

        user, _, server = msg.chat_id.partition("@")
        pn = await client.get_pn_from_lid(build_jid(user, server))  # type: ignore[attr-defined]
        alt = f"{pn.User}@{pn.Server}" if pn and getattr(pn, "User", None) else None
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("wa_lid_resolve_failed", error=repr(exc)[:120])
        return False
    if not alt:
        return False
    store = TenantScope(rt.db, msg.tenant_id)
    alt_row = repo.chat_get(store, "wa", alt)
    if alt_row is not None and alt_row["is_whitelisted"]:
        repo.chat_set_whitelisted_by_chat_id(store, "wa", msg.chat_id)
        rt.audit.note("wa_whitelist_linked", lid=msg.chat_id, phone=alt)
        return True
    return False


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


async def _send_reply_now(rt: Runtime, platform: str, chat_id: str,
                          chat_kind: str, text: str,
                          reply_to: str | None = None) -> None:
    """Send an auto-reply straight to the chat as the owner (no confirm gate —
    auto-reply chats are the owner's explicit opt-in). reply_to quotes/tags the
    message being answered (Telegram)."""
    if platform == "tg":
        from ..tools.telegram import _send_group_executor, _send_private_executor

        executor = _send_private_executor if chat_kind == "private" else _send_group_executor
        await executor(rt, {"chat_id": chat_id, "text": text, "reply_to": reply_to})
    elif platform == "wa":
        from ..tools.whatsapp import _send_executor

        await _send_executor(rt, {"chat_jid": chat_id, "text": text})


async def _auto_reply(rt: Runtime, router, batch: list[InboundMessage],
                      chat_row, persona_block: str) -> None:
    """The 'answering agent': a cheap, TOOL-LESS model writes a short reply to
    EACH incoming message and it's sent to the chat. Unlike the smart agent it
    never calls tools — it just talks — so it can answer everyone in a group
    quickly and cheaply. Respects the chat's reply-delay policy."""
    from ..scheduler.delays import compute_due

    first = batch[0]
    chat_name = chat_row["name"] if chat_row and chat_row["name"] else first.chat_id
    delay_json = chat_row["delay_policy_json"] if chat_row else None
    sent = 0
    _MAX = 25  # runaway guard: never fire more than this many replies per batch
    for m in batch:
        if m.is_from_me or not (m.text or "").strip():
            continue
        # Skip emoji / sticker / punctuation-only messages — nothing to answer,
        # and it saves a model call. isalnum() is true for Hebrew/Cyrillic too.
        if not any(c.isalnum() for c in (m.text or "")):
            continue
        if sent >= _MAX:
            rt.audit.note("auto_reply_capped", chat=first.chat_id, cap=_MAX)
            break
        system = (
            f"You are replying AS THE OWNER in the chat \"{chat_name}\". The owner "
            "wants EVERY message here answered — always write a reply, even to "
            "small talk. Keep it short and natural, in the SAME language as the "
            "message. No greetings or sign-offs. Reply with ONLY the reply text."
            + persona_block
        )
        user = (f"From {m.sender_name or m.sender_id}:\n{wrap_untrusted(m.text)}")
        try:
            res = await router.complete(
                purpose="reply", system=system,
                messages=[ChatMessage(role="user", text=user)],
                max_tokens=300, chat_pk=chat_row["id"] if chat_row else None)
        except ProviderError as exc:
            rt.audit.note("auto_reply_failed", chat=first.chat_id, error=str(exc)[:120])
            continue
        reply = (res.text or "").strip()
        if not reply or reply.lower().startswith("<skip"):
            continue
        due = compute_due(delay_json) if delay_json else None
        if due is not None and chat_row is not None:
            repo.pending_reply_create(
                TenantScope(rt.db, first.tenant_id),
                chat_pk=chat_row["id"], draft_text=reply,
                due_at=due.strftime("%Y-%m-%d %H:%M:%S"), reply_to=m.msg_id)
        else:
            try:
                await _send_reply_now(rt, first.platform, first.chat_id,
                                      first.chat_kind, reply, reply_to=m.msg_id)
            except Exception as exc:  # noqa: BLE001
                rt.audit.note("auto_reply_send_failed", chat=first.chat_id,
                              error=repr(exc)[:150])
                continue
        sent += 1
    rt.audit.note("auto_reply_done", chat=first.chat_id, replied=sent, batch=len(batch),
                  queued=bool(delay_json))


async def _process_batch(rt: Runtime, batch: list[InboundMessage]) -> None:
    from ..llm.router import Router

    router: Router = rt.router  # type: ignore[assignment]
    registry: Registry = rt.registry  # type: ignore[assignment]
    first = batch[0]
    # A batch is per (tenant, chat) by construction — see InboundMessage.chat_key.
    tenant = tenant_context(rt, first.tenant_id)
    store = tenant.scope
    chat_row = repo.chat_get(store, first.platform, first.chat_id)
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
    rt.audit.note("triage", tenant_id=first.tenant_id, chat=first.chat_id,
                  platform=first.platform, verdict=verdict.action,
                  confidence=verdict.confidence, reason=verdict.reason)
    if verdict.action == "ignore":
        return

    persona_block = ""
    if persona_id is not None:
        persona = repo.persona_by_id(store, persona_id)
        if persona:
            persona_block = f"\nPersona for replies in this chat:\n{persona['system_prompt']}\n"

    # The dumb "answering agent" (cheap, tool-less) writes a reply to EACH
    # sender. The smart tool-agent below runs only for calendar/actions.
    if verdict.action in ("respond", "both") and auto_reply:
        await _auto_reply(rt, router, batch, chat_row, persona_block)
    if verdict.action not in ("calendar", "both"):
        return

    task_lines = [
        "- If the message(s) contain a concrete event, create it with "
        "calendar_create_event (check calendar_check_conflicts first when a "
        "specific time is given)."
    ]

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

    # Per-(chat, persona) rolling context so ongoing conversations have memory.
    history = []
    if chat_pk is not None:
        history = [
            ChatMessage(role=r["role"], text=r["content"])  # type: ignore[arg-type]
            for r in repo.context_get(store, chat_pk, persona_id, limit=16)
            if r["role"] in ("user", "assistant") and r["content"]
        ]
    history.append(ChatMessage(role="user", text=user_text))

    ctx = ToolContext(
        rt=rt, scope="inbound", origin_chat_pk=chat_pk, tenant=tenant,
        extras={"source_msg_id": first.msg_id, "platform": first.platform,
                "chat_id": first.chat_id},
    )
    try:
        result = await run_agent(
            router, registry, ctx,
            system=system,
            messages=history,
            purpose="agent",
            chat_pk=chat_pk,
        )
        if chat_pk is not None:
            repo.context_add(store, chat_pk, persona_id, "user", combined[:4000])
            repo.context_add(store, chat_pk, persona_id, "assistant",
                             (result or "")[:4000])
            repo.context_prune(store, chat_pk, persona_id)
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
            store = TenantScope(rt.db, msg.tenant_id)
            chat_pk = repo.chat_upsert(store, msg.platform, msg.chat_id,
                                       msg.chat_name, msg.chat_kind)
            chat_row = repo.chat_get(store, msg.platform, msg.chat_id)

            if msg.is_edit:
                before = repo.message_mark_edited(store, msg.platform, msg.chat_id,
                                                  msg.msg_id, msg.text)
                await tglog.log_change(rt, msg, before)
            elif msg.is_delete:
                before = repo.message_mark_deleted(store, msg.platform, msg.chat_id,
                                                   msg.msg_id)
                await tglog.log_change(rt, msg, before)
            else:
                repo.message_upsert(store, msg, chat_pk)
                # Identifiers only — the app fetches content over the
                # authenticated API, so no message text enters the fan-out.
                rt.events.publish(
                    "message.new", tenant_id=msg.tenant_id, chat_pk=chat_pk,
                    platform=msg.platform, chat_id=msg.chat_id, msg_id=msg.msg_id,
                    sender=msg.sender_name or msg.sender_id,
                )

            allowed, reason = decide(rt, chat_row, msg)
            # WhatsApp LID<->phone: honour a whitelist set on the counterpart id.
            if not allowed and reason == "not_whitelisted" and msg.platform == "wa":
                if await _wa_whitelisted_via_counterpart(rt, msg):
                    allowed, reason = True, "whitelisted_via_lid"
            rt.audit.gate(platform=msg.platform, chat_id=msg.chat_id,
                          sender_id=msg.sender_id, allowed=allowed, reason=reason,
                          text=msg.text, tenant_id=msg.tenant_id)
            if allowed:
                debouncer.add(msg)
        except Exception as exc:  # noqa: BLE001 — consumer must survive anything
            rt.audit.note("pipeline_error", error=repr(exc)[:300])
        finally:
            rt.bus.task_done()
