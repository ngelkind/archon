"""Chats + per-chat gates + message history.

Reads project ``repo`` rows straight to DTOs. Writes never touch the DB here:
each PATCH field dispatches the SAME tool the agent and the Telegram bot use, so
a gate flipped from the phone is audited identically to one flipped by voice.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ...db import repo
from ...db.tenancy import TenantScope
from ..auth import require_tenant
from ..ctx import tenant_ctx
from ..schemas import Chat, ChatPatch, ChatPatchResponse, Message

router = APIRouter(dependencies=[Depends(require_tenant)], tags=["chats"])


def _chat_dto(row: sqlite3.Row) -> Chat:
    raw_delay = row["delay_policy_json"]
    try:
        delay = json.loads(raw_delay) if raw_delay else None
    except (TypeError, ValueError):
        delay = None
    return Chat(
        pk=int(row["id"]),
        platform=row["platform"],
        chat_id=row["chat_id"],
        name=row["name"],
        kind=row["kind"],
        is_whitelisted=bool(row["is_whitelisted"]),
        auto_reply=bool(row["auto_reply"]),
        image_recognition=bool(row["image_recognition"]),
        send_policy=row["send_policy"],
        delay_policy=delay,
        persona_id=row["persona_id"],
        log_deletes=bool(row["log_deletes"]),
        capture_media=bool(row["capture_media"]),
        last_seen_at=row["last_seen_at"],
    )


def _get_chat_or_404(rt, pk: int, tenant_id: int) -> sqlite3.Row:
    row = repo.chat_get_by_pk(TenantScope(rt.db, tenant_id), pk)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown chat")
    return row


@router.get("/chats", response_model=list[Chat])
async def list_chats(
    request: Request,
    platform: str | None = Query(default=None, pattern="^(wa|tg|gmail)$"),
    whitelisted_only: bool = False,
    tenant_id: int = Depends(require_tenant),
) -> list[Chat]:
    rt = request.app.state.rt
    rows = repo.chat_list(TenantScope(rt.db, tenant_id), platform=platform,
                          whitelisted_only=whitelisted_only)
    return [_chat_dto(r) for r in rows]


@router.get("/chats/{pk}", response_model=Chat)
async def get_chat(pk: int, request: Request,
                   tenant_id: int = Depends(require_tenant)) -> Chat:
    return _chat_dto(_get_chat_or_404(request.app.state.rt, pk, tenant_id))


def _planned_calls(patch: ChatPatch, platform: str, chat_id: str) -> list[tuple[str, str, dict]]:
    """(field, tool_name, args) for each field the client actually set."""
    target = {"platform": platform, "chat_id": chat_id}
    calls: list[tuple[str, str, dict[str, Any]]] = []
    if patch.is_whitelisted is not None:
        calls.append(("is_whitelisted",
                      "whitelist_add" if patch.is_whitelisted else "whitelist_remove",
                      dict(target)))
    if patch.auto_reply is not None:
        calls.append(("auto_reply", "auto_reply_set",
                      {**target, "enabled": patch.auto_reply}))
    if patch.image_recognition is not None:
        calls.append(("image_recognition", "image_recognition_set",
                      {**target, "enabled": patch.image_recognition}))
    if patch.send_policy is not None:
        calls.append(("send_policy", "send_policy_set",
                      {**target, "policy": patch.send_policy}))
    if patch.log_deletes is not None:
        calls.append(("log_deletes", "chat_log_policy_set",
                      {**target, "enabled": patch.log_deletes}))
    if patch.capture_media is not None:
        calls.append(("capture_media",
                      "capture_add" if patch.capture_media else "capture_remove",
                      dict(target)))
    if patch.persona_name is not None:
        calls.append(("persona_name", "persona_assign",
                      {**target, "persona_name": patch.persona_name}))
    if patch.delay_mode is not None:
        calls.append(("delay_policy", "delay_policy_set", {
            **target, "mode": patch.delay_mode,
            "min_s": patch.delay_min_s or 0, "max_s": patch.delay_max_s or 0,
        }))
    return calls


@router.patch("/chats/{pk}", response_model=ChatPatchResponse)
async def patch_chat(pk: int, patch: ChatPatch, request: Request,
                     tenant_id: int = Depends(require_tenant)) -> ChatPatchResponse:
    rt = request.app.state.rt
    row = _get_chat_or_404(rt, pk, tenant_id)
    calls = _planned_calls(patch, row["platform"], row["chat_id"])
    if not calls:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="no fields to update")
    ctx = tenant_ctx(rt, tenant_id)
    applied: dict[str, str] = {}
    for field, tool, args in calls:
        applied[field] = await rt.registry.dispatch(ctx, tool, args)
    return ChatPatchResponse(chat=_chat_dto(_get_chat_or_404(rt, pk, tenant_id)),
                             applied=applied)


@router.get("/chats/{pk}/messages", response_model=list[Message])
# TODO(TOS-REVIEW): All platforms — exposes stored third-party message content over the API — review before launch
async def chat_messages(
    pk: int, request: Request, limit: int = Query(default=50, ge=1, le=500),
    tenant_id: int = Depends(require_tenant),
) -> list[Message]:
    rt = request.app.state.rt
    _get_chat_or_404(rt, pk, tenant_id)
    return [
        Message(
            id=int(r["id"]), msg_id=r["msg_id"], sender_id=r["sender_id"],
            sender_name=r["sender_name"], is_from_me=bool(r["is_from_me"]),
            ts=r["ts"], text=r["text"], media_path=r["media_path"],
            edited_text=r["edited_text"], edited_at=r["edited_at"],
            deleted_at=r["deleted_at"],
        )
        for r in repo.message_history(TenantScope(rt.db, tenant_id), pk, limit=limit)
    ]
