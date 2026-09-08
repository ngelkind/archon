"""Chat/settings tools — including whitelist-by-approximate-name, the reason
the LLM has chat-listing tools at all."""

from __future__ import annotations

import difflib
import json

from ..db import repo
from .registry import Registry, ToolContext

#: The only send policies the gates understand. Anything else is treated as
#: "confirm" by the send paths — a policy nobody recognises must fail CLOSED.
SEND_POLICIES = frozenset({"free", "confirm"})

#: Settings the tools understand. A typo used to write a dead row and report
#: ok:true. llm.model.* / llm.key.* are prefixes and validated separately.
KNOWN_SETTING_KEYS = frozenset({
    "gmail.triage_enabled", "gmail.include_self", "log.channel_id", "log.redact_pii",
    "log.groups_default", "log.coalesce_seconds", "calendar.default_id",
    "monitor.private_chats", "monitor.groups", "monitor.include_own_messages",
    "wa.send_delay_min_s", "wa.send_delay_max_s", "capture.all_dms",
    "llm.active_provider", "llm.daily_budget_usd", "whatsapp.enabled",
})

_SECRET_KEY_MARKERS = (".key.", "llm.key", "secret", "token")


def _mask(key: str, value):
    return "•••" if any(m in key for m in _SECRET_KEY_MARKERS) else value


def effective_send_policy(row) -> str:
    policy = row["send_policy"] if row is not None else None
    return policy if policy in SEND_POLICIES else "confirm"


def _find_chat(store, platform: str | None, approx_name: str) -> list[dict]:
    """Fuzzy-match a chat by name across the chats registry."""
    rows = repo.chat_list(store, platform=platform)
    scored = []
    query = approx_name.casefold().strip()
    for r in rows:
        name = (r["name"] or r["chat_id"]).casefold()
        ratio = difflib.SequenceMatcher(None, query, name).ratio()
        if query in name:
            ratio = max(ratio, 0.85)
        if ratio >= 0.45:
            scored.append((ratio, r))
    scored.sort(key=lambda t: -t[0])
    return [
        {"platform": r["platform"], "chat_id": r["chat_id"], "name": r["name"],
         "kind": r["kind"], "score": round(s, 2),
         "whitelisted": bool(r["is_whitelisted"])}
        for s, r in scored[:8]
    ]


def _get_chat_or_error(store, platform: str, chat_id: str):
    row = repo.chat_get(store, platform, chat_id)
    if row is None:
        raise ValueError(f"unknown chat {platform}:{chat_id} — use chat_list/wa_list_groups first")
    return row


def register(registry: Registry) -> None:
    @registry.tool(
        "chat_list",
        "List chats Archon has seen, optionally filtered by platform "
        "(wa | tg | gmail), with their policies.",
        {
            "type": "object",
            "properties": {"platform": {"type": "string", "enum": ["wa", "tg", "gmail"]}},
        },
    )
    async def chat_list(ctx: ToolContext, platform: str = "") -> str:
        rows = repo.chat_list(ctx.store, platform=platform or None)
        shown = rows[:200]
        chats = [
            {"platform": r["platform"], "chat_id": r["chat_id"], "name": r["name"],
             "kind": r["kind"], "whitelisted": bool(r["is_whitelisted"]),
             "auto_reply": bool(r["auto_reply"]), "send_policy": r["send_policy"],
             "log_deletes": bool(r["log_deletes"]),
             "image_recognition": bool(r["image_recognition"])}
            for r in shown
        ]
        return json.dumps({"total": len(rows), "shown": len(shown), "chats": chats},
                          ensure_ascii=False)

    @registry.tool(
        "chat_find",
        "Fuzzy-find a chat by approximate name (the owner rarely knows exact "
        "names). Returns scored candidates; ask the owner if ambiguous.",
        {
            "type": "object",
            "properties": {
                "approx_name": {"type": "string"},
                "platform": {"type": "string", "enum": ["wa", "tg", "gmail"]},
            },
            "required": ["approx_name"],
        },
    )
    async def chat_find(ctx: ToolContext, approx_name: str, platform: str = "") -> str:
        return json.dumps(_find_chat(ctx.store, platform or None, approx_name),
                          ensure_ascii=False)

    @registry.tool(
        "whitelist_add",
        "Whitelist a chat for processing (auto calendar events etc.) by its "
        "exact chat_id. Resolve approximate names with chat_find first; if "
        "several candidates score similarly, ask the owner which one.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
            },
            "required": ["platform", "chat_id"],
        },
        sensitive=True,
    )
    async def whitelist_add(ctx: ToolContext, platform: str, chat_id: str) -> str:
        row = _get_chat_or_error(ctx.store, platform, chat_id)
        repo.chat_set_field(ctx.store, row["id"], "is_whitelisted", 1)
        return json.dumps({"ok": True, "chat": row["name"] or chat_id, "whitelisted": True})

    @registry.tool(
        "whitelist_remove",
        "Remove a chat from the whitelist.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
            },
            "required": ["platform", "chat_id"],
        },
        sensitive=True,
    )
    async def whitelist_remove(ctx: ToolContext, platform: str, chat_id: str) -> str:
        row = _get_chat_or_error(ctx.store, platform, chat_id)
        repo.chat_set_field(ctx.store, row["id"], "is_whitelisted", 0)
        return json.dumps({"ok": True, "chat": row["name"] or chat_id, "whitelisted": False})

    @registry.tool(
        "send_policy_set",
        "Set a chat's send policy: 'free' (Archon may send without asking) or "
        "'confirm' (owner approves each send). New chats default to confirm.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg", "gmail"]},
                "chat_id": {"type": "string"},
                "policy": {"type": "string", "enum": ["free", "confirm"]},
            },
            "required": ["platform", "chat_id", "policy"],
        },
        sensitive=True,
    )
    async def send_policy_set(ctx: ToolContext, platform: str, chat_id: str,
                              policy: str) -> str:
        if policy not in SEND_POLICIES:
            # The JSON-schema enum is advisory to the model; an unknown value
            # written here would have bypassed the confirm gate entirely.
            return json.dumps({"error": f"policy must be one of {sorted(SEND_POLICIES)}",
                               "got": policy})
        row = _get_chat_or_error(ctx.store, platform, chat_id)
        repo.chat_set_field(ctx.store, row["id"], "send_policy", policy)
        return json.dumps({"ok": True, "chat": row["name"] or chat_id, "send_policy": policy})

    @registry.tool(
        "auto_reply_set",
        "Enable/disable automatic replies in a chat (requires whitelist too).",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["platform", "chat_id", "enabled"],
        },
        sensitive=True,
    )
    async def auto_reply_set(ctx: ToolContext, platform: str, chat_id: str,
                             enabled: bool) -> str:
        row = _get_chat_or_error(ctx.store, platform, chat_id)
        repo.chat_set_field(ctx.store, row["id"], "auto_reply", int(enabled))
        return json.dumps({"ok": True, "chat": row["name"] or chat_id, "auto_reply": enabled})

    @registry.tool(
        "image_recognition_set",
        "Enable/disable image understanding for a chat (images are downloaded "
        "and described to the agent for context).",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["platform", "chat_id", "enabled"],
        },
        sensitive=True,
    )
    async def image_recognition_set(ctx: ToolContext, platform: str, chat_id: str,
                                    enabled: bool) -> str:
        row = _get_chat_or_error(ctx.store, platform, chat_id)
        repo.chat_set_field(ctx.store, row["id"], "image_recognition", int(enabled))
        return json.dumps({"ok": True, "chat": row["name"] or chat_id,
                           "image_recognition": enabled})

    @registry.tool(
        "chat_log_policy_set",
        "Enable/disable deleted/edited-message logging for a chat.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["platform", "chat_id", "enabled"],
        },
        sensitive=True,
    )
    async def chat_log_policy_set(ctx: ToolContext, platform: str, chat_id: str,
                                  enabled: bool) -> str:
        row = _get_chat_or_error(ctx.store, platform, chat_id)
        repo.chat_set_field(ctx.store, row["id"], "log_deletes", int(enabled))
        return json.dumps({"ok": True, "chat": row["name"] or chat_id, "log_deletes": enabled})

    @registry.tool(
        "settings_get",
        "Read a global setting by key (or list all keys when key is empty).",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
        },
    )
    async def settings_get(ctx: ToolContext, key: str = "") -> str:
        if key:
            return json.dumps({key: _mask(key, repo.setting_get(ctx.store, key))},
                              ensure_ascii=False)
        rows = repo.setting_all(ctx.store)
        redacted = {}
        for r in rows:
            redacted[r["key"]] = _mask(r["key"], json.loads(r["value_json"]))
        return json.dumps(redacted, ensure_ascii=False)

    @registry.tool(
        "settings_set",
        "Set a global setting (JSON value). Known keys include "
        "gmail.triage_enabled, wa.send_delay_min_s/max_s, log.channel_id, "
        "log.redact_pii, calendar.default_id.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value_json": {"type": "string",
                               "description": "JSON-encoded value, e.g. 'true' or '\"primary\"'"},
            },
            "required": ["key", "value_json"],
        },
        sensitive=True,
    )
    async def settings_set(ctx: ToolContext, key: str, value_json: str) -> str:
        if key not in KNOWN_SETTING_KEYS and not key.startswith(("llm.model.", "llm.key.")):
            import difflib

            near = difflib.get_close_matches(key, sorted(KNOWN_SETTING_KEYS), n=3)
            return json.dumps({"error": f"unknown setting key: {key}",
                               "did_you_mean": near}, ensure_ascii=False)
        try:
            value = json.loads(value_json)
        except json.JSONDecodeError:
            value = value_json  # treat as plain string
        repo.setting_set(ctx.store, key, value)
        return json.dumps({"ok": True, "key": key, "value": _mask(key, value)},
                          ensure_ascii=False)
