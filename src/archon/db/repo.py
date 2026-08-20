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
    existing values — only name and last_seen are refreshed.

    New chats: edit/delete logging defaults ON for DMs, OFF for groups/channels
    (groups are opt-in via chat_log_policy_set — otherwise it's just spam)."""
    default_log = 1 if kind in ("private", "email") else 0
    db.execute(
        "INSERT INTO chats (platform, chat_id, name, kind, log_deletes, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(platform, chat_id) DO UPDATE SET "
        "name = COALESCE(excluded.name, chats.name), last_seen_at = excluded.last_seen_at",
        (platform, chat_id, name, kind, default_log, _now()),
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
        "capture_media",
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


# --- pending actions (the confirm gate) -------------------------------------

def pending_action_create(
    db: Db, *, kind: str, payload_json: str, chat_pk: int | None, expires_at: str
) -> int:
    cur = db.execute(
        "INSERT INTO pending_actions (kind, payload_json, chat_pk, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (kind, payload_json, chat_pk, expires_at),
    )
    return int(cur.lastrowid)


def pending_action_get(db: Db, action_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM pending_actions WHERE id = ?", (action_id,))


def pending_action_claim(db: Db, action_id: int, status: str) -> bool:
    """Atomic single-use transition out of 'pending'. Returns True for exactly
    one caller — whichever channel (phone / Telegram / push) gets there first;
    every other caller sees False and reports 'already handled'."""
    cur = db.execute(
        "UPDATE pending_actions SET status = ? WHERE id = ? AND status = 'pending'",
        (status, action_id),
    )
    return cur.rowcount == 1


def pending_action_expire(db: Db, action_id: int) -> None:
    db.execute(
        "UPDATE pending_actions SET status = 'expired' WHERE id = ? AND status = 'pending'",
        (action_id,),
    )


def pending_action_set_owner_msg(db: Db, action_id: int, owner_msg_id: int) -> None:
    db.execute("UPDATE pending_actions SET owner_msg_id = ? WHERE id = ?",
               (owner_msg_id, action_id))


def pending_action_list(db: Db, status: str = "pending", limit: int = 50) -> list[sqlite3.Row]:
    return db.query(
        "SELECT * FROM pending_actions WHERE status = ? ORDER BY id DESC LIMIT ?",
        (status, limit),
    )


# --- read projections for the control API -----------------------------------

def setting_all(db: Db) -> list[sqlite3.Row]:
    return db.query("SELECT key, value_json FROM settings ORDER BY key")


def contact_list(db: Db, limit: int = 500) -> list[sqlite3.Row]:
    return db.query(
        "SELECT name, phone, source FROM contacts ORDER BY name LIMIT ?", (limit,)
    )


def contact_counts(db: Db) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT COUNT(*) AS entries, COUNT(DISTINCT phone) AS unique_numbers FROM contacts"
    )


def schedule_list(db: Db, limit: int = 50) -> list[sqlite3.Row]:
    return db.query(
        "SELECT s.id, s.platform, s.chat_pk, c.chat_id, c.name, s.text, s.due_at, "
        "s.status FROM scheduled_messages s JOIN chats c ON c.id = s.chat_pk "
        "ORDER BY s.id DESC LIMIT ?",
        (limit,),
    )


# --- api devices / pairing --------------------------------------------------

def api_device_create(
    db: Db, *, name: str | None, token_hash: str,
    device_pubkey: str | None = None, push_endpoint: str | None = None,
) -> int:
    cur = db.execute(
        "INSERT INTO api_devices (name, token_hash, device_pubkey, push_endpoint) "
        "VALUES (?, ?, ?, ?)",
        (name, token_hash, device_pubkey, push_endpoint),
    )
    return int(cur.lastrowid)


def api_device_by_token_hash(db: Db, token_hash: str) -> sqlite3.Row | None:
    """A live (non-revoked) device for this bearer hash, or None."""
    return db.query_one(
        "SELECT * FROM api_devices WHERE token_hash = ? AND revoked_at IS NULL",
        (token_hash,),
    )


def api_device_touch(db: Db, device_id: int) -> None:
    db.execute("UPDATE api_devices SET last_seen_at = ? WHERE id = ?", (_now(), device_id))


def api_device_revoke(db: Db, device_id: int) -> None:
    db.execute(
        "UPDATE api_devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_now(), device_id),
    )


def api_device_list(db: Db) -> list[sqlite3.Row]:
    return db.query("SELECT * FROM api_devices ORDER BY created_at DESC")


def api_pair_code_create(db: Db, *, code_hash: str, expires_at: str) -> None:
    db.execute(
        "INSERT INTO api_pair_codes (code_hash, expires_at) VALUES (?, ?) "
        "ON CONFLICT(code_hash) DO UPDATE SET "
        "expires_at = excluded.expires_at, used_at = NULL",
        (code_hash, expires_at),
    )


def api_pair_code_consume(db: Db, code_hash: str) -> bool:
    """Single-use redemption: atomically mark the code used iff it is unused and
    unexpired. Returns True exactly once per valid code (whichever caller wins)."""
    now = _now()
    cur = db.execute(
        "UPDATE api_pair_codes SET used_at = ? "
        "WHERE code_hash = ? AND used_at IS NULL AND expires_at >= ?",
        (now, code_hash, now),
    )
    return cur.rowcount == 1


# --- multi-tenant accounts (product mode) -----------------------------------

def user_create(
    db: Db, *, email: str, password_hash: str, display_name: str | None
) -> int:
    """Insert a new account; caller normalizes ``email`` (lowercase) and has
    already checked for a duplicate. Raises sqlite3.IntegrityError on a racing
    duplicate (UNIQUE email), which the router maps to a 409."""
    cur = db.execute(
        "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
        (email, password_hash, display_name),
    )
    return int(cur.lastrowid)


def user_by_email(db: Db, email: str) -> sqlite3.Row | None:
    """Live (non-disabled) account for this normalized email, or None."""
    return db.query_one(
        "SELECT * FROM users WHERE email = ? AND disabled_at IS NULL", (email,)
    )


def user_by_id(db: Db, user_id: int) -> sqlite3.Row | None:
    """Live (non-disabled) account by id, or None."""
    return db.query_one(
        "SELECT * FROM users WHERE id = ? AND disabled_at IS NULL", (user_id,)
    )


def user_touch_login(db: Db, user_id: int) -> None:
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), user_id))


def refresh_token_create(
    db: Db, *, token_hash: str, user_id: int, expires_at: str
) -> None:
    db.execute(
        "INSERT INTO refresh_tokens (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash, user_id, expires_at),
    )


def refresh_token_get(db: Db, token_hash: str) -> sqlite3.Row | None:
    """The stored row for this hash (live or not); the caller checks
    revoked_at / expires_at so it can tell 'unknown' from 'reused/expired'."""
    return db.query_one(
        "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
    )


def refresh_token_revoke(db: Db, token_hash: str) -> bool:
    """Atomic single-use revoke. Returns True exactly once per live token; a
    second call (token reuse / double logout) returns False."""
    cur = db.execute(
        "UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
        (_now(), token_hash),
    )
    return cur.rowcount == 1


def refresh_tokens_revoke_all(db: Db, user_id: int) -> int:
    """Revoke every live refresh token for a user (e.g. reuse detected / global
    logout). Returns the number revoked."""
    cur = db.execute(
        "UPDATE refresh_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
        (_now(), user_id),
    )
    return int(cur.rowcount)


# --- audit ------------------------------------------------------------------

def audit_add(db: Db, actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
    db.execute(
        "INSERT INTO audit (actor, action, detail_json) VALUES (?, ?, ?)",
        (actor, action, json.dumps(detail, ensure_ascii=False, default=str) if detail else None),
    )
