"""Typed accessors over the schema.

Grows with the milestones; keep every SQL statement in this module so the
rest of the codebase never writes raw SQL.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..models import InboundMessage
from . import Db


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


# --- settings -------------------------------------------------------------

def setting_get(db: Db, key: str, default: Any = None) -> Any:
    row = db.query_one("SELECT value_json FROM settings WHERE key = ?", (key,))
    return json.loads(row["value_json"]) if row else default


def setting_set(db: Db, key: str, value: Any) -> None:
    db.execute(
        "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
        "updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), _now()),
    )


# --- chats ----------------------------------------------------------------

def chat_upsert(
    db: Db, platform: str, chat_id: str, name: str | None, kind: str
) -> int:
    """Register/refresh a chat; returns its primary key. Policies keep their
    existing values — only name and last_seen are refreshed."""
    db.execute(
        "INSERT INTO chats (platform, chat_id, name, kind, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(platform, chat_id) DO UPDATE SET "
        "name = COALESCE(excluded.name, chats.name), last_seen_at = excluded.last_seen_at",
        (platform, chat_id, name, kind, _now()),
    )
    row = db.query_one(
        "SELECT id FROM chats WHERE platform = ? AND chat_id = ?", (platform, chat_id)
    )
    assert row is not None
    return int(row["id"])


def chat_get(db: Db, platform: str, chat_id: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM chats WHERE platform = ? AND chat_id = ?", (platform, chat_id)
    )


def chat_get_by_pk(db: Db, pk: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM chats WHERE id = ?", (pk,))


def chat_list(
    db: Db, platform: str | None = None, whitelisted_only: bool = False
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM chats WHERE 1=1"
    params: list[Any] = []
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if whitelisted_only:
        sql += " AND is_whitelisted = 1"
    sql += " ORDER BY last_seen_at DESC"
    return db.query(sql, tuple(params))


def chat_set_field(db: Db, pk: int, field: str, value: Any) -> None:
    allowed = {
        "name", "is_whitelisted", "auto_reply", "image_recognition",
        "send_policy", "delay_policy_json", "persona_id", "log_deletes",
    }
    if field not in allowed:
        raise ValueError(f"chat field not settable: {field}")
    db.execute(f"UPDATE chats SET {field} = ? WHERE id = ?", (value, pk))  # noqa: S608


# --- message cache ----------------------------------------------------------

def message_upsert(db: Db, msg: InboundMessage, chat_pk: int) -> None:
    db.execute(
        "INSERT INTO messages (chat_pk, platform, chat_id, msg_id, source, sender_id, "
        "sender_name, is_from_me, ts, text, media_path, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(platform, chat_id, msg_id) DO NOTHING",
        (
            chat_pk, msg.platform, msg.chat_id, msg.msg_id, msg.source,
            msg.sender_id, msg.sender_name, int(msg.is_from_me),
            msg.ts.strftime("%Y-%m-%d %H:%M:%S"), msg.text,
            msg.media[0].local_path if msg.media else None,
            json.dumps(msg.raw, ensure_ascii=False, default=str) if msg.raw else None,
        ),
    )


def message_get(db: Db, platform: str, chat_id: str, msg_id: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM messages WHERE platform = ? AND chat_id = ? AND msg_id = ?",
        (platform, chat_id, msg_id),
    )


def message_mark_edited(
    db: Db, platform: str, chat_id: str, msg_id: str, new_text: str | None
) -> sqlite3.Row | None:
    """Record an edit; returns the row as it was BEFORE the edit (for diffs)."""
    before = message_get(db, platform, chat_id, msg_id)
    db.execute(
        "UPDATE messages SET edited_text = ?, edited_at = ? "
        "WHERE platform = ? AND chat_id = ? AND msg_id = ?",
        (new_text, _now(), platform, chat_id, msg_id),
    )
    return before


def message_mark_deleted(
    db: Db, platform: str, chat_id: str, msg_id: str
) -> sqlite3.Row | None:
    """Record a deletion; returns the cached row (the 'before' content)."""
    before = message_get(db, platform, chat_id, msg_id)
    db.execute(
        "UPDATE messages SET deleted_at = ? "
        "WHERE platform = ? AND chat_id = ? AND msg_id = ?",
        (_now(), platform, chat_id, msg_id),
    )
    return before


def message_history(db: Db, chat_pk: int, limit: int = 50) -> list[sqlite3.Row]:
    return db.query(
        "SELECT * FROM messages WHERE chat_pk = ? ORDER BY id DESC LIMIT ?",
        (chat_pk, limit),
    )


# --- llm cost tracking ------------------------------------------------------

def llm_call_record(
    db: Db,
    *,
    purpose: str,
    provider: str,
    model: str,
    in_tokens: int = 0,
    out_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.0,
    tool_call_count: int = 0,
    latency_ms: int | None = None,
    ok: bool = True,
    chat_pk: int | None = None,
) -> None:
    db.execute(
        "INSERT INTO llm_calls (purpose, provider, model, in_tokens, out_tokens, "
        "cache_read_tokens, cache_write_tokens, cost_usd, tool_call_count, "
        "latency_ms, ok, chat_pk) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            purpose, provider, model, in_tokens, out_tokens,
            cache_read_tokens, cache_write_tokens, round(cost_usd, 8),
            tool_call_count, latency_ms, int(ok), chat_pk,
        ),
    )


def llm_cost_since(db: Db, since_expr: str) -> sqlite3.Row | None:
    """Aggregate cost/tokens since a SQLite datetime modifier, e.g. '-1 day'."""
    return db.query_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
        "COALESCE(SUM(in_tokens), 0) AS in_tok, COALESCE(SUM(out_tokens), 0) AS out_tok "
        "FROM llm_calls WHERE ts >= datetime('now', ?)",
        (since_expr,),
    )


def llm_cost_breakdown(db: Db, since_expr: str) -> list[sqlite3.Row]:
    return db.query(
        "SELECT provider, model, purpose, COUNT(*) AS calls, "
        "COALESCE(SUM(cost_usd), 0) AS cost "
        "FROM llm_calls WHERE ts >= datetime('now', ?) "
        "GROUP BY provider, model, purpose ORDER BY cost DESC",
        (since_expr,),
    )


# --- agent conversation contexts ---------------------------------------------

def context_add(db: Db, chat_pk: int, persona_id: int | None, role: str, content: str) -> None:
    db.execute(
        "INSERT INTO context_messages (chat_pk, persona_id, role, content) VALUES (?, ?, ?, ?)",
        (chat_pk, persona_id, role, content),
    )


def context_get(
    db: Db, chat_pk: int, persona_id: int | None, limit: int = 40
) -> list[sqlite3.Row]:
    rows = db.query(
        "SELECT * FROM context_messages WHERE chat_pk = ? AND persona_id IS ? "
        "ORDER BY id DESC LIMIT ?",
        (chat_pk, persona_id, limit),
    )
    return list(reversed(rows))


def context_prune(db: Db, chat_pk: int, persona_id: int | None, keep: int = 80) -> None:
    db.execute(
        "DELETE FROM context_messages WHERE chat_pk = ? AND persona_id IS ? AND id NOT IN ("
        "SELECT id FROM context_messages WHERE chat_pk = ? AND persona_id IS ? "
        "ORDER BY id DESC LIMIT ?)",
        (chat_pk, persona_id, chat_pk, persona_id, keep),
    )


def context_clear(db: Db, chat_pk: int, persona_id: int | None = None) -> None:
    db.execute(
        "DELETE FROM context_messages WHERE chat_pk = ? AND persona_id IS ?",
        (chat_pk, persona_id),
    )


# --- audit ------------------------------------------------------------------

def audit_add(db: Db, actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
    db.execute(
        "INSERT INTO audit (actor, action, detail_json) VALUES (?, ?, ?)",
        (actor, action, json.dumps(detail, ensure_ascii=False, default=str) if detail else None),
    )
