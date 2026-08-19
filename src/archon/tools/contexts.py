"""Persona and context tools — per-chat agent personalities ('talk to this
client like a negotiator') with separate conversation memory per persona."""

from __future__ import annotations

import json

from ..db import repo
from .registry import Registry, ToolContext


def register(registry: Registry) -> None:
    @registry.tool(
        "persona_create",
        "Create a persona: a named system-prompt style Archon uses when "
        "writing in chats it is assigned to (e.g. 'negotiator', 'warm-friend').",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "system_prompt": {"type": "string",
                                  "description": "How to write: tone, goals, boundaries"},
                "model_override": {"type": "string",
                                   "description": "optional specific model"},
            },
            "required": ["name", "system_prompt"],
        },
        sensitive=True,
    )
    async def persona_create(ctx: ToolContext, name: str, system_prompt: str,
                             model_override: str = "") -> str:
        ctx.rt.db.execute(
            "INSERT INTO personas (name, system_prompt, model_override) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET system_prompt = excluded.system_prompt, "
            "model_override = excluded.model_override",
            (name.strip(), system_prompt, model_override or None),
        )
        return json.dumps({"ok": True, "persona": name.strip()})

    @registry.tool(
        "persona_list",
        "List personas and which chats they are assigned to.",
    )
    async def persona_list(ctx: ToolContext) -> str:
        personas = ctx.rt.db.query("SELECT * FROM personas ORDER BY name")
        out = []
        for p in personas:
            chats = ctx.rt.db.query(
                "SELECT platform, chat_id, name FROM chats WHERE persona_id = ?",
                (p["id"],),
            )
            out.append({
                "name": p["name"],
                "system_prompt": p["system_prompt"][:300],
                "model_override": p["model_override"],
                "assigned_chats": [c["name"] or c["chat_id"] for c in chats],
            })
        return json.dumps(out, ensure_ascii=False)

    @registry.tool(
        "persona_delete",
        "Delete a persona (assigned chats revert to the default style).",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        sensitive=True,
    )
    async def persona_delete(ctx: ToolContext, name: str) -> str:
        cur = ctx.rt.db.execute("DELETE FROM personas WHERE name = ?", (name.strip(),))
        return json.dumps({"ok": cur.rowcount > 0, "persona": name})

    @registry.tool(
        "persona_assign",
        "Assign a persona to a chat (empty persona_name clears the assignment). "
        "The chat keeps a SEPARATE conversation context per persona.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg", "gmail"]},
                "chat_id": {"type": "string"},
                "persona_name": {"type": "string"},
            },
            "required": ["platform", "chat_id", "persona_name"],
        },
        sensitive=True,
    )
    async def persona_assign(ctx: ToolContext, platform: str, chat_id: str,
                             persona_name: str) -> str:
        row = repo.chat_get(ctx.rt.db, platform, chat_id)
        if row is None:
            return json.dumps({"error": "unknown chat — use chat_find first"})
        if not persona_name.strip():
            repo.chat_set_field(ctx.rt.db, row["id"], "persona_id", None)
            return json.dumps({"ok": True, "chat": row["name"] or chat_id, "persona": None})
        persona = ctx.rt.db.query_one("SELECT id FROM personas WHERE name = ?",
                                      (persona_name.strip(),))
        if persona is None:
            return json.dumps({"error": f"no persona named {persona_name!r}"})
        repo.chat_set_field(ctx.rt.db, row["id"], "persona_id", persona["id"])
        return json.dumps({"ok": True, "chat": row["name"] or chat_id,
                           "persona": persona_name.strip()})

    @registry.tool(
        "context_show",
        "Show the stored agent conversation context for a chat.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg", "gmail"]},
                "chat_id": {"type": "string"},
            },
            "required": ["platform", "chat_id"],
        },
    )
    async def context_show(ctx: ToolContext, platform: str, chat_id: str) -> str:
        row = repo.chat_get(ctx.rt.db, platform, chat_id)
        if row is None:
            return json.dumps({"error": "unknown chat"})
        rows = repo.context_get(ctx.rt.db, row["id"], row["persona_id"], limit=20)
        return json.dumps([{"role": r["role"], "content": r["content"][:400]}
                           for r in rows], ensure_ascii=False)

    @registry.tool(
        "context_clear",
        "Clear the stored agent context for a chat (fresh start).",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg", "gmail"]},
                "chat_id": {"type": "string"},
            },
            "required": ["platform", "chat_id"],
        },
        sensitive=True,
    )
    async def context_clear(ctx: ToolContext, platform: str, chat_id: str) -> str:
        row = repo.chat_get(ctx.rt.db, platform, chat_id)
        if row is None:
            return json.dumps({"error": "unknown chat"})
        repo.context_clear(ctx.rt.db, row["id"], row["persona_id"])
        return json.dumps({"ok": True})
