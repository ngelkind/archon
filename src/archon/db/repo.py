"""Typed accessors over the schema.

Grows with the milestones; keep every SQL statement in this module so the
rest of the codebase never writes raw SQL.

**Tenancy.** Every per-user accessor takes ``store``, which is either a raw
``Db`` (the legacy single-user path — resolves to the owner tenant) or a
``TenantScope`` (the multi-tenant product path). The tenant is then read from
the scope and injected into the SQL here; it is never taken from a caller
argument, so no caller can address another tenant's rows by passing an id.
See ``db/tenancy.py`` for the reasoning and the tripwire.

Accessors for genuinely global tables (users, refresh_tokens, api_pair_codes)
still take a plain ``Db`` — they are identity/process state, not per-user data.
``audit`` is tenanted with a NULLABLE tenant_id: NULL marks a SYSTEM row that
belongs to the process rather than a person (see 009_audit_tenant.sql).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..models import InboundMessage
from . import Db
from .tenancy import OWNER_TENANT_ID, TenantScope, as_scope

Store = Db | TenantScope


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


# --- settings -------------------------------------------------------------

def setting_get(store: Store, key: str, default: Any = None) -> Any:
    sc = as_scope(store)
    row = sc.query_one(
        "SELECT value_json FROM settings WHERE tenant_id = ? AND key = ?",
        (sc.tenant_id, key),
    )
    return json.loads(row["value_json"]) if row else default


def setting_set(store: Store, key: str, value: Any) -> None:
    sc = as_scope(store)
    sc.execute(
        "INSERT INTO settings (tenant_id, key, value_json, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, key) DO UPDATE SET value_json = excluded.value_json, "
        "updated_at = excluded.updated_at",
        (sc.tenant_id, key, json.dumps(value, ensure_ascii=False), _now()),
    )


# --- chats ----------------------------------------------------------------

def chat_upsert(
    store: Store, platform: str, chat_id: str, name: str | None, kind: str
) -> int:
    """Register/refresh a chat; returns its primary key. Policies keep their
    existing values — only name and last_seen are refreshed.

    New chats: edit/delete logging defaults ON for DMs, OFF for groups/channels
    (groups are opt-in via chat_log_policy_set — otherwise it's just spam)."""
    sc = as_scope(store)
    default_log = 1 if kind in ("private", "email") else 0
    sc.execute(
        "INSERT INTO chats (tenant_id, platform, chat_id, name, kind, log_deletes, "
        "last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, platform, chat_id) DO UPDATE SET "
        "name = COALESCE(excluded.name, chats.name), last_seen_at = excluded.last_seen_at",
        (sc.tenant_id, platform, chat_id, name, kind, default_log, _now()),
    )
    row = sc.query_one(
        "SELECT id FROM chats WHERE tenant_id = ? AND platform = ? AND chat_id = ?",
        (sc.tenant_id, platform, chat_id),
    )
    assert row is not None
    return int(row["id"])


def chat_get(store: Store, platform: str, chat_id: str) -> sqlite3.Row | None:
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM chats WHERE tenant_id = ? AND platform = ? AND chat_id = ?",
        (sc.tenant_id, platform, chat_id),
    )


def chat_get_by_pk(store: Store, pk: int) -> sqlite3.Row | None:
    """None when the chat belongs to another tenant — indistinguishable from
    'does not exist', which is what callers should surface."""
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM chats WHERE id = ? AND tenant_id = ?", (pk, sc.tenant_id)
    )


def chat_list(
    store: Store, platform: str | None = None, whitelisted_only: bool = False
) -> list[sqlite3.Row]:
    sc = as_scope(store)
    sql = "SELECT * FROM chats WHERE tenant_id = ?"
    params: list[Any] = [sc.tenant_id]
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if whitelisted_only:
        sql += " AND is_whitelisted = 1"
    sql += " ORDER BY last_seen_at DESC"
    return sc.query(sql, tuple(params))


def chat_set_field(store: Store, pk: int, field: str, value: Any) -> None:
    allowed = {
        "name", "is_whitelisted", "auto_reply", "image_recognition",
        "send_policy", "delay_policy_json", "persona_id", "log_deletes",
        "capture_media",
    }
    if field not in allowed:
        raise ValueError(f"chat field not settable: {field}")
    sc = as_scope(store)
    sc.execute(
        f"UPDATE chats SET {field} = ? WHERE id = ? AND tenant_id = ?",  # noqa: S608
        (value, pk, sc.tenant_id),
    )


# --- message cache ----------------------------------------------------------

def message_upsert(store: Store, msg: InboundMessage, chat_pk: int) -> None:
    sc = as_scope(store)
    sc.execute(
        "INSERT INTO messages (tenant_id, chat_pk, platform, chat_id, msg_id, source, "
        "sender_id, sender_name, is_from_me, ts, text, media_path, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, platform, chat_id, msg_id) DO NOTHING",
        (
            sc.tenant_id, chat_pk, msg.platform, msg.chat_id, msg.msg_id, msg.source,
            msg.sender_id, msg.sender_name, int(msg.is_from_me),
            msg.ts.strftime("%Y-%m-%d %H:%M:%S"), msg.text,
            msg.media[0].local_path if msg.media else None,
            json.dumps(msg.raw, ensure_ascii=False, default=str) if msg.raw else None,
        ),
    )


def message_get(
    store: Store, platform: str, chat_id: str, msg_id: str
) -> sqlite3.Row | None:
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM messages WHERE tenant_id = ? AND platform = ? AND chat_id = ? "
        "AND msg_id = ?",
        (sc.tenant_id, platform, chat_id, msg_id),
    )


def message_mark_edited(
    store: Store, platform: str, chat_id: str, msg_id: str, new_text: str | None
) -> sqlite3.Row | None:
    """Record an edit; returns the row as it was BEFORE the edit (for diffs)."""
    sc = as_scope(store)
    before = message_get(sc, platform, chat_id, msg_id)
    sc.execute(
        "UPDATE messages SET edited_text = ?, edited_at = ? "
        "WHERE tenant_id = ? AND platform = ? AND chat_id = ? AND msg_id = ?",
        (new_text, _now(), sc.tenant_id, platform, chat_id, msg_id),
    )
    return before


def message_mark_deleted(
    store: Store, platform: str, chat_id: str, msg_id: str
) -> sqlite3.Row | None:
    """Record a deletion; returns the cached row (the 'before' content)."""
    sc = as_scope(store)
    before = message_get(sc, platform, chat_id, msg_id)
    sc.execute(
        "UPDATE messages SET deleted_at = ? "
        "WHERE tenant_id = ? AND platform = ? AND chat_id = ? AND msg_id = ?",
        (_now(), sc.tenant_id, platform, chat_id, msg_id),
    )
    return before


def message_history(store: Store, chat_pk: int, limit: int = 50) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT * FROM messages WHERE tenant_id = ? AND chat_pk = ? "
        "ORDER BY id DESC LIMIT ?",
        (sc.tenant_id, chat_pk, limit),
    )


# --- llm cost tracking ------------------------------------------------------

def llm_call_record(
    store: Store,
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
    sc = as_scope(store)
    sc.execute(
        "INSERT INTO llm_calls (tenant_id, purpose, provider, model, in_tokens, "
        "out_tokens, cache_read_tokens, cache_write_tokens, cost_usd, tool_call_count, "
        "latency_ms, ok, chat_pk) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sc.tenant_id, purpose, provider, model, in_tokens, out_tokens,
            cache_read_tokens, cache_write_tokens, round(cost_usd, 8),
            tool_call_count, latency_ms, int(ok), chat_pk,
        ),
    )


def llm_cost_since(store: Store, since_expr: str) -> sqlite3.Row | None:
    """Aggregate cost/tokens since a SQLite datetime modifier, e.g. '-1 day'."""
    sc = as_scope(store)
    return sc.query_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
        "COALESCE(SUM(in_tokens), 0) AS in_tok, COALESCE(SUM(out_tokens), 0) AS out_tok "
        "FROM llm_calls WHERE tenant_id = ? AND ts >= datetime('now', ?)",
        (sc.tenant_id, since_expr),
    )


def llm_cost_breakdown(store: Store, since_expr: str) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT provider, model, purpose, COUNT(*) AS calls, "
        "COALESCE(SUM(cost_usd), 0) AS cost "
        "FROM llm_calls WHERE tenant_id = ? AND ts >= datetime('now', ?) "
        "GROUP BY provider, model, purpose ORDER BY cost DESC",
        (sc.tenant_id, since_expr),
    )


# --- agent conversation contexts ---------------------------------------------

def context_add(
    store: Store, chat_pk: int, persona_id: int | None, role: str, content: str
) -> None:
    sc = as_scope(store)
    sc.execute(
        "INSERT INTO context_messages (tenant_id, chat_pk, persona_id, role, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (sc.tenant_id, chat_pk, persona_id, role, content),
    )


def context_get(
    store: Store, chat_pk: int, persona_id: int | None, limit: int = 40
) -> list[sqlite3.Row]:
    sc = as_scope(store)
    rows = sc.query(
        "SELECT * FROM context_messages WHERE tenant_id = ? AND chat_pk = ? "
        "AND persona_id IS ? ORDER BY id DESC LIMIT ?",
        (sc.tenant_id, chat_pk, persona_id, limit),
    )
    return list(reversed(rows))


def context_prune(
    store: Store, chat_pk: int, persona_id: int | None, keep: int = 80
) -> None:
    sc = as_scope(store)
    sc.execute(
        "DELETE FROM context_messages WHERE tenant_id = ? AND chat_pk = ? "
        "AND persona_id IS ? AND id NOT IN ("
        "SELECT id FROM context_messages WHERE tenant_id = ? AND chat_pk = ? "
        "AND persona_id IS ? ORDER BY id DESC LIMIT ?)",
        (sc.tenant_id, chat_pk, persona_id, sc.tenant_id, chat_pk, persona_id, keep),
    )


def context_clear(store: Store, chat_pk: int, persona_id: int | None = None) -> None:
    sc = as_scope(store)
    sc.execute(
        "DELETE FROM context_messages WHERE tenant_id = ? AND chat_pk = ? "
        "AND persona_id IS ?",
        (sc.tenant_id, chat_pk, persona_id),
    )


# --- pending actions (the confirm gate) -------------------------------------

def pending_action_create(
    store: Store, *, kind: str, payload_json: str, chat_pk: int | None, expires_at: str
) -> int:
    sc = as_scope(store)
    cur = sc.execute(
        "INSERT INTO pending_actions (tenant_id, kind, payload_json, chat_pk, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (sc.tenant_id, kind, payload_json, chat_pk, expires_at),
    )
    return int(cur.lastrowid)


def pending_action_get(store: Store, action_id: int) -> sqlite3.Row | None:
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM pending_actions WHERE id = ? AND tenant_id = ?",
        (action_id, sc.tenant_id),
    )


def pending_action_claim(store: Store, action_id: int, status: str) -> bool:
    """Atomic single-use transition out of 'pending'. Returns True for exactly
    one caller — whichever channel (phone / Telegram / push) gets there first;
    every other caller sees False and reports 'already handled'.

    Tenant-scoped, so one tenant cannot resolve another's approval."""
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE pending_actions SET status = ? "
        "WHERE id = ? AND tenant_id = ? AND status = 'pending'",
        (status, action_id, sc.tenant_id),
    )
    return cur.rowcount == 1


def pending_action_expire(store: Store, action_id: int) -> None:
    sc = as_scope(store)
    sc.execute(
        "UPDATE pending_actions SET status = 'expired' "
        "WHERE id = ? AND tenant_id = ? AND status = 'pending'",
        (action_id, sc.tenant_id),
    )


def pending_action_set_owner_msg(store: Store, action_id: int, owner_msg_id: int) -> None:
    sc = as_scope(store)
    sc.execute(
        "UPDATE pending_actions SET owner_msg_id = ? WHERE id = ? AND tenant_id = ?",
        (owner_msg_id, action_id, sc.tenant_id),
    )


def pending_action_list(
    store: Store, status: str = "pending", limit: int = 50
) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT * FROM pending_actions WHERE tenant_id = ? AND status = ? "
        "ORDER BY id DESC LIMIT ?",
        (sc.tenant_id, status, limit),
    )


# --- read projections for the control API -----------------------------------

def setting_all(store: Store) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT key, value_json FROM settings WHERE tenant_id = ? ORDER BY key",
        (sc.tenant_id,),
    )


def contact_list(store: Store, limit: int = 500) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT name, phone, source FROM contacts WHERE tenant_id = ? "
        "ORDER BY name LIMIT ?",
        (sc.tenant_id, limit),
    )


def contact_counts(store: Store) -> sqlite3.Row | None:
    sc = as_scope(store)
    return sc.query_one(
        "SELECT COUNT(*) AS entries, COUNT(DISTINCT phone) AS unique_numbers "
        "FROM contacts WHERE tenant_id = ?",
        (sc.tenant_id,),
    )


def schedule_list(store: Store, limit: int = 50) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT s.id, s.platform, s.chat_pk, c.chat_id, c.name, s.text, s.due_at, "
        "s.status FROM scheduled_messages s JOIN chats c ON c.id = s.chat_pk "
        "WHERE s.tenant_id = ? ORDER BY s.id DESC LIMIT ?",
        (sc.tenant_id, limit),
    )


# --- api devices / pairing --------------------------------------------------

def api_device_create(
    store: Store, *, name: str | None, token_hash: str,
    device_pubkey: str | None = None, push_endpoint: str | None = None,
) -> int:
    sc = as_scope(store)
    cur = sc.execute(
        "INSERT INTO api_devices (tenant_id, name, token_hash, device_pubkey, "
        "push_endpoint) VALUES (?, ?, ?, ?, ?)",
        (sc.tenant_id, name, token_hash, device_pubkey, push_endpoint),
    )
    return int(cur.lastrowid)


def api_device_by_token_hash(db: Db, token_hash: str) -> sqlite3.Row | None:
    """A live (non-revoked) device for this bearer hash, or None.

    Deliberately NOT tenant-scoped: this is the lookup that *establishes* which
    tenant is calling, so it cannot presuppose one. Token hashes are 256-bit
    random values, globally unique; the row's ``tenant_id`` is the answer, and
    every subsequent query in the request runs under that tenant's scope.
    """
    return db.query_one(
        "SELECT * FROM api_devices WHERE token_hash = ? AND revoked_at IS NULL",
        (token_hash,),
    )


def api_device_touch(store: Store, device_id: int) -> None:
    sc = as_scope(store)
    sc.execute(
        "UPDATE api_devices SET last_seen_at = ? WHERE id = ? AND tenant_id = ?",
        (_now(), device_id, sc.tenant_id),
    )


def api_device_revoke(store: Store, device_id: int) -> None:
    sc = as_scope(store)
    sc.execute(
        "UPDATE api_devices SET revoked_at = ? "
        "WHERE id = ? AND tenant_id = ? AND revoked_at IS NULL",
        (_now(), device_id, sc.tenant_id),
    )


def api_device_list(store: Store) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT * FROM api_devices WHERE tenant_id = ? ORDER BY created_at DESC",
        (sc.tenant_id,),
    )


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

def audit_add(db: Db, actor: str, action: str, detail: dict[str, Any] | None = None,
              tenant_id: int | None = None) -> None:
    """Append an audit row. ``tenant_id`` None means a SYSTEM row (startup,
    subsystem crash) that belongs to the process rather than to a person."""
    db.execute(
        "INSERT INTO audit (tenant_id, actor, action, detail_json) VALUES (?, ?, ?, ?)",
        (tenant_id, actor, action,
         json.dumps(detail, ensure_ascii=False, default=str) if detail else None),
    )


def audit_query(store: Store, *, contains: str = "", limit: int = 30) -> list[sqlite3.Row]:
    """This tenant's audit rows, newest first.

    The owner additionally sees SYSTEM rows (``tenant_id IS NULL``): on the
    personal bot, process events like startup and subsystem_crash are the
    owner's own operational history, and hiding them would be a regression in
    the single-user ``audit_query`` tool. A product tenant sees only their own
    rows — never another tenant's, and never the process's.
    """
    sc = as_scope(store)
    sql = "SELECT ts, actor, action, detail_json FROM audit WHERE ("
    params: list[Any] = []
    if sc.tenant_id == OWNER_TENANT_ID:
        sql += "tenant_id = ? OR tenant_id IS NULL)"
    else:
        sql += "tenant_id = ?)"
    params.append(sc.tenant_id)
    if contains:
        sql += " AND (action LIKE ? OR detail_json LIKE ?)"
        params += [f"%{contains}%", f"%{contains}%"]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return sc.query(sql, tuple(params))


# --- personas ---------------------------------------------------------------

def persona_upsert(store: Store, name: str, system_prompt: str,
                   model_override: str | None = None) -> None:
    """Create or update a persona by name, within the tenant."""
    sc = as_scope(store)
    sc.execute(
        "INSERT INTO personas (tenant_id, name, system_prompt, model_override) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, name) DO UPDATE SET "
        "system_prompt = excluded.system_prompt, "
        "model_override = excluded.model_override",
        (sc.tenant_id, name, system_prompt, model_override),
    )


def persona_chats(store: Store, persona_id: int) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT platform, chat_id, name FROM chats WHERE tenant_id = ? AND persona_id = ?",
        (sc.tenant_id, persona_id),
    )


def persona_by_name(store: Store, name: str) -> sqlite3.Row | None:
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM personas WHERE tenant_id = ? AND name = ?", (sc.tenant_id, name)
    )


def persona_by_id(store: Store, persona_id: int) -> sqlite3.Row | None:
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM personas WHERE id = ? AND tenant_id = ?",
        (persona_id, sc.tenant_id),
    )


def persona_list(store: Store) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT * FROM personas WHERE tenant_id = ? ORDER BY name", (sc.tenant_id,)
    )


def persona_delete(store: Store, name: str) -> int:
    sc = as_scope(store)
    cur = sc.execute(
        "DELETE FROM personas WHERE tenant_id = ? AND name = ?", (sc.tenant_id, name)
    )
    return int(cur.rowcount)


# --- scheduled messages ------------------------------------------------------

def schedule_create(store: Store, *, platform: str, chat_pk: int, text: str | None,
                    due_at: str, media_path: str | None = None,
                    status: str = "pending", tg_native_id: int | None = None,
                    result: str | None = None) -> int:
    sc = as_scope(store)
    cur = sc.execute(
        "INSERT INTO scheduled_messages (tenant_id, platform, chat_pk, text, media_path, "
        "due_at, status, tg_native_id, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sc.tenant_id, platform, chat_pk, text, media_path, due_at, status,
         tg_native_id, result),
    )
    return int(cur.lastrowid)


def schedule_due_all_tenants(db: Db, now: str) -> list[sqlite3.Row]:
    """Due scheduled messages ACROSS ALL TENANTS, each row carrying its own
    ``tenant_id``.

    Deliberately cross-tenant and named so: the scheduler is process-wide
    infrastructure that must fire every tenant's messages, then act under that
    row's tenant. Scoping it to one tenant would silently strand everyone
    else's schedules. Callers must use ``TenantScope(db, row["tenant_id"])``
    for any follow-up write.
    """
    return db.query(
        "SELECT s.*, c.platform AS c_platform, c.chat_id AS c_chat_id, c.name AS c_name "
        "FROM scheduled_messages s JOIN chats c ON c.id = s.chat_pk "
        "WHERE s.status = 'pending' AND s.due_at <= ? ORDER BY s.due_at",
        (now,),
    )


def schedule_set_status(store: Store, schedule_id: int, status: str,
                        result: str | None = None) -> None:
    sc = as_scope(store)
    sc.execute(
        "UPDATE scheduled_messages SET status = ?, result = ? "
        "WHERE id = ? AND tenant_id = ?",
        (status, result, schedule_id, sc.tenant_id),
    )


def schedule_cancel(store: Store, schedule_id: int) -> bool:
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE scheduled_messages SET status = 'cancelled' "
        "WHERE id = ? AND tenant_id = ? AND status = 'pending'",
        (schedule_id, sc.tenant_id),
    )
    return cur.rowcount > 0


# --- pending (delayed) replies ----------------------------------------------

def pending_reply_create(store: Store, *, chat_pk: int, draft_text: str, due_at: str,
                         reply_to: str | None = None) -> int:
    sc = as_scope(store)
    cur = sc.execute(
        "INSERT INTO pending_replies (tenant_id, chat_pk, draft_text, due_at, reply_to) "
        "VALUES (?, ?, ?, ?, ?)",
        (sc.tenant_id, chat_pk, draft_text, due_at, reply_to),
    )
    return int(cur.lastrowid)


def pending_reply_due_all_tenants(db: Db, now: str) -> list[sqlite3.Row]:
    """Due delayed replies ACROSS ALL TENANTS — see
    :func:`schedule_due_all_tenants` for why this one is intentionally
    unscoped."""
    return db.query(
        "SELECT p.*, c.platform AS c_platform, c.chat_id AS c_chat_id, c.name AS c_name "
        "FROM pending_replies p JOIN chats c ON c.id = p.chat_pk "
        "WHERE p.status = 'pending' AND p.due_at <= ? ORDER BY p.due_at",
        (now,),
    )


def pending_reply_list(store: Store, limit: int = 30) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT p.id, c.platform, c.chat_id, c.name, p.draft_text, p.due_at, p.status "
        "FROM pending_replies p JOIN chats c ON c.id = p.chat_pk "
        "WHERE p.tenant_id = ? AND p.status = 'pending' ORDER BY p.due_at LIMIT ?",
        (sc.tenant_id, limit),
    )


def pending_reply_set_status(store: Store, reply_id: int, status: str) -> bool:
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE pending_replies SET status = ? WHERE id = ? AND tenant_id = ?",
        (status, reply_id, sc.tenant_id),
    )
    return cur.rowcount > 0


def pending_reply_cancel(store: Store, reply_id: int) -> bool:
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE pending_replies SET status = 'cancelled' "
        "WHERE id = ? AND tenant_id = ? AND status = 'pending'",
        (reply_id, sc.tenant_id),
    )
    return cur.rowcount > 0


# --- gmail sync state --------------------------------------------------------

def gmail_state_get(store: Store, key: str) -> str | None:
    sc = as_scope(store)
    row = sc.query_one(
        "SELECT value FROM gmail_state WHERE tenant_id = ? AND key = ?",
        (sc.tenant_id, key),
    )
    return row["value"] if row else None


def gmail_state_set(store: Store, key: str, value: str) -> None:
    sc = as_scope(store)
    sc.execute(
        "INSERT INTO gmail_state (tenant_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(tenant_id, key) DO UPDATE SET value = excluded.value",
        (sc.tenant_id, key, value),
    )


# --- sub-bots ----------------------------------------------------------------

def sub_bot_create(store: Store, *, token: str, bot_username: str,
                   platform_scope: str) -> int:
    sc = as_scope(store)
    cur = sc.execute(
        "INSERT INTO sub_bots (tenant_id, token, bot_username, platform_scope) "
        "VALUES (?, ?, ?, ?)",
        (sc.tenant_id, token, bot_username, platform_scope),
    )
    return int(cur.lastrowid)


def sub_bot_list(store: Store, enabled_only: bool = False) -> list[sqlite3.Row]:
    sc = as_scope(store)
    sql = "SELECT * FROM sub_bots WHERE tenant_id = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY id"
    return sc.query(sql, (sc.tenant_id,))


def sub_bot_list_all_tenants(db: Db) -> list[sqlite3.Row]:
    """Sub-bots ACROSS ALL TENANTS, each row carrying its ``tenant_id``.

    Intentionally unscoped: the sub-bot supervisor is process-wide and must run
    every tenant's bots. Each spawned bot then works under its own tenant scope.
    """
    return db.query("SELECT * FROM sub_bots ORDER BY id")


def sub_bot_set_enabled(store: Store, bot_id: int, enabled: bool) -> bool:
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE sub_bots SET enabled = ? WHERE id = ? AND tenant_id = ?",
        (int(enabled), bot_id, sc.tenant_id),
    )
    return cur.rowcount > 0


def sub_bot_delete(store: Store, bot_id: int) -> bool:
    sc = as_scope(store)
    cur = sc.execute(
        "DELETE FROM sub_bots WHERE id = ? AND tenant_id = ?", (bot_id, sc.tenant_id)
    )
    return cur.rowcount > 0


# --- calendar events created -------------------------------------------------

def event_created_add(store: Store, *, chat_pk: int | None, source_msg_id: str | None,
                      gcal_event_id: str, calendar_id: str, title: str,
                      start_ts: str | None, end_ts: str | None) -> int:
    sc = as_scope(store)
    cur = sc.execute(
        "INSERT INTO events_created (tenant_id, chat_pk, source_msg_id, gcal_event_id, "
        "calendar_id, title, start_ts, end_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sc.tenant_id, chat_pk, source_msg_id, gcal_event_id, calendar_id, title,
         start_ts, end_ts),
    )
    return int(cur.lastrowid)


def event_created_mark_cancelled(store: Store, gcal_event_id: str) -> None:
    sc = as_scope(store)
    sc.execute(
        "UPDATE events_created SET status = 'cancelled' "
        "WHERE tenant_id = ? AND gcal_event_id = ?",
        (sc.tenant_id, gcal_event_id),
    )


# --- contacts (rows for the directory matcher) -------------------------------

def contact_rows(store: Store) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT name, phone, norm, source FROM contacts WHERE tenant_id = ?",
        (sc.tenant_id,),
    )


def contact_add(store: Store, name: str, phone: str, norm: str, source: str) -> int:
    """INSERT OR IGNORE; returns rowcount (0 when the pair already existed)."""
    sc = as_scope(store)
    cur = sc.execute(
        "INSERT OR IGNORE INTO contacts (tenant_id, name, phone, norm, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (sc.tenant_id, name, phone, norm, source),
    )
    return int(cur.rowcount)


# --- misc scoped helpers -----------------------------------------------------

def chat_set_whitelisted_by_chat_id(store: Store, platform: str, chat_id: str) -> None:
    sc = as_scope(store)
    sc.execute(
        "UPDATE chats SET is_whitelisted = 1 "
        "WHERE tenant_id = ? AND platform = ? AND chat_id = ?",
        (sc.tenant_id, platform, chat_id),
    )


def chat_list_capture_armed(store: Store) -> list[sqlite3.Row]:
    sc = as_scope(store)
    return sc.query(
        "SELECT platform, chat_id, name, kind FROM chats "
        "WHERE tenant_id = ? AND capture_media = 1",
        (sc.tenant_id,),
    )


def message_cache_outgoing(store: Store, *, chat_pk: int, platform: str,
                           chat_id: str, msg_id: str, source: str,
                           text: str | None) -> None:
    """Cache a message WE sent, so history/edit/delete tracking sees it too."""
    sc = as_scope(store)
    sc.execute(
        "INSERT OR IGNORE INTO messages (tenant_id, chat_pk, platform, chat_id, msg_id, "
        "source, sender_id, is_from_me, ts, text) "
        "VALUES (?, ?, ?, ?, ?, ?, 'me', 1, datetime('now'), ?)",
        (sc.tenant_id, chat_pk, platform, chat_id, msg_id, source, text),
    )


def message_exists(store: Store, platform: str, msg_id: str) -> bool:
    sc = as_scope(store)
    return sc.query_one(
        "SELECT 1 FROM messages WHERE tenant_id = ? AND platform = ? AND msg_id = ?",
        (sc.tenant_id, platform, msg_id),
    ) is not None


def message_search(store: Store, where_tail: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Scoped free-form message query for the history/deleted-message tools.

    ``where_tail`` is a WHERE fragment supplied by a caller in this codebase (no
    user input is interpolated); the tenant predicate is prepended here so it
    cannot be left out.
    """
    sc = as_scope(store)
    return sc.query(
        f"SELECT * FROM messages WHERE tenant_id = ? AND {where_tail}",  # noqa: S608
        (sc.tenant_id, *params),
    )


# --- per-tenant integration credentials --------------------------------------

def integration_cred_upsert(store: Store, *, provider: str, secret_envelope: str,
                            account_label: str | None = None,
                            scopes: str | None = None) -> None:
    """Store (or replace) this tenant's credential for a provider.

    Re-linking clears ``revoked_at`` — consenting again is exactly how a user
    recovers from a revoked or expired token.
    """
    sc = as_scope(store)
    sc.execute(
        "INSERT INTO integration_credentials (tenant_id, provider, account_label, "
        "secret_envelope, scopes) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, provider) DO UPDATE SET "
        "account_label = excluded.account_label, "
        "secret_envelope = excluded.secret_envelope, scopes = excluded.scopes, "
        "updated_at = datetime('now'), revoked_at = NULL",
        (sc.tenant_id, provider, account_label, secret_envelope, scopes),
    )


def integration_cred_get(store: Store, provider: str) -> sqlite3.Row | None:
    """This tenant's live credential for a provider, or None if absent/revoked."""
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM integration_credentials "
        "WHERE tenant_id = ? AND provider = ? AND revoked_at IS NULL",
        (sc.tenant_id, provider),
    )


def integration_cred_list(store: Store) -> list[sqlite3.Row]:
    """Link status per provider, without the secret."""
    sc = as_scope(store)
    return sc.query(
        "SELECT provider, account_label, scopes, created_at, updated_at, revoked_at "
        "FROM integration_credentials WHERE tenant_id = ? ORDER BY provider",
        (sc.tenant_id,),
    )


def integration_cred_revoke(store: Store, provider: str) -> bool:
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE integration_credentials SET revoked_at = datetime('now') "
        "WHERE tenant_id = ? AND provider = ? AND revoked_at IS NULL",
        (sc.tenant_id, provider),
    )
    return cur.rowcount > 0


# --- OAuth state (CSRF + which tenant a callback belongs to) -----------------

def oauth_state_create(db: Db, *, state: str, tenant_id: int, provider: str,
                       expires_at: str) -> None:
    """Not tenant-scoped by the caller's scope on purpose: the row IS the record
    of which tenant this flow belongs to, written before the redirect."""
    db.execute(
        "INSERT INTO oauth_states (state, tenant_id, provider, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (state, tenant_id, provider, expires_at),
    )


def oauth_state_consume(db: Db, state: str, provider: str) -> sqlite3.Row | None:
    """Atomically spend a state value; returns its row exactly once.

    Single-use and time-bounded: a replayed callback, or one carrying a state
    this server never issued, gets None — which is what stops an attacker
    pasting their own authorization code into someone else's session.
    """
    now = _now()
    cur = db.execute(
        "UPDATE oauth_states SET used_at = ? "
        "WHERE state = ? AND provider = ? AND used_at IS NULL AND expires_at >= ?",
        (now, state, provider, now),
    )
    if cur.rowcount != 1:
        return None
    return db.query_one("SELECT * FROM oauth_states WHERE state = ?", (state,))


def oauth_state_purge_expired(db: Db) -> int:
    cur = db.execute("DELETE FROM oauth_states WHERE expires_at < ?", (_now(),))
    return int(cur.rowcount)


def integration_linked_tenants(db: Db, provider: str) -> list[int]:
    """Every tenant with a live credential for a provider, ACROSS ALL TENANTS.

    Deliberately unscoped and named so: background pollers are process-wide and
    must find every linked tenant, then work under each one's own scope. A
    tenant-scoped version would silently poll only the owner.
    """
    return [
        int(r["tenant_id"])
        for r in db.query(
            "SELECT tenant_id FROM integration_credentials "
            "WHERE provider = ? AND revoked_at IS NULL ORDER BY tenant_id",
            (provider,),
        )
    ]


# --- telegram links (per-tenant Business connection) -------------------------

def telegram_link_upsert(store: Store, *, tg_user_id: str,
                         tg_username: str | None = None,
                         tg_name: str | None = None) -> int:
    """Bind a Telegram identity to this tenant, replacing any previous one.

    Re-linking a different Telegram account revokes the old row rather than
    editing it, so the audit trail keeps who was connected when.
    """
    sc = as_scope(store)
    sc.execute(
        "UPDATE telegram_links SET revoked_at = ? "
        "WHERE tenant_id = ? AND revoked_at IS NULL AND tg_user_id <> ?",
        (_now(), sc.tenant_id, tg_user_id),
    )
    existing = sc.query_one(
        "SELECT id FROM telegram_links "
        "WHERE tenant_id = ? AND tg_user_id = ? AND revoked_at IS NULL",
        (sc.tenant_id, tg_user_id),
    )
    if existing is not None:
        sc.execute(
            "UPDATE telegram_links SET tg_username = ?, tg_name = ? "
            "WHERE id = ? AND tenant_id = ?",
            (tg_username, tg_name, existing["id"], sc.tenant_id),
        )
        return int(existing["id"])
    cur = sc.execute(
        "INSERT INTO telegram_links (tenant_id, tg_user_id, tg_username, tg_name) "
        "VALUES (?, ?, ?, ?)",
        (sc.tenant_id, tg_user_id, tg_username, tg_name),
    )
    return int(cur.lastrowid)


def telegram_link_get(store: Store) -> sqlite3.Row | None:
    """This tenant's live Telegram link, if any."""
    sc = as_scope(store)
    return sc.query_one(
        "SELECT * FROM telegram_links WHERE tenant_id = ? AND revoked_at IS NULL",
        (sc.tenant_id,),
    )


def telegram_link_revoke(store: Store) -> bool:
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE telegram_links SET revoked_at = ?, is_enabled = 0 "
        "WHERE tenant_id = ? AND revoked_at IS NULL",
        (_now(), sc.tenant_id),
    )
    return cur.rowcount > 0


def telegram_link_set_connection(store: Store, *, business_connection_id: str | None,
                                 enabled: bool) -> bool:
    """Record the Business connection Telegram just issued for this tenant."""
    sc = as_scope(store)
    cur = sc.execute(
        "UPDATE telegram_links SET business_connection_id = ?, is_enabled = ?, "
        "connected_at = ? WHERE tenant_id = ? AND revoked_at IS NULL",
        (business_connection_id, int(enabled), _now() if enabled else None,
         sc.tenant_id),
    )
    return cur.rowcount > 0


# The next two are the ROUTING lookups: they answer "whose message is this?"
# before any tenant scope exists, so they are deliberately unscoped — exactly
# like api_device_by_token_hash. Both keys are issued by Telegram, not by a
# caller, and each resolves to at most one live tenant.

def telegram_tenant_by_connection(db: Db, business_connection_id: str) -> int | None:
    row = db.query_one(
        "SELECT tenant_id FROM telegram_links "
        "WHERE business_connection_id = ? AND revoked_at IS NULL",
        (business_connection_id,),
    )
    return int(row["tenant_id"]) if row else None


def telegram_tenant_by_user_id(db: Db, tg_user_id: str) -> int | None:
    row = db.query_one(
        "SELECT tenant_id FROM telegram_links "
        "WHERE tg_user_id = ? AND revoked_at IS NULL",
        (str(tg_user_id),),
    )
    return int(row["tenant_id"]) if row else None


def telegram_links_all_tenants(db: Db) -> list[sqlite3.Row]:
    """Every live link, ACROSS ALL TENANTS — for admin/status views only."""
    return db.query(
        "SELECT * FROM telegram_links WHERE revoked_at IS NULL ORDER BY tenant_id"
    )


def telegram_link_code_create(db: Db, *, code_hash: str, tenant_id: int,
                              expires_at: str) -> None:
    """Issued by the app, redeemed in the bot. Unscoped for the same reason as
    oauth_states: the row records which tenant the in-flight handshake is for."""
    db.execute(
        "INSERT INTO telegram_link_codes (code_hash, tenant_id, expires_at) "
        "VALUES (?, ?, ?) ON CONFLICT(code_hash) DO UPDATE SET "
        "tenant_id = excluded.tenant_id, expires_at = excluded.expires_at, "
        "used_at = NULL",
        (code_hash, tenant_id, expires_at),
    )


def telegram_link_code_consume(db: Db, code_hash: str) -> int | None:
    """Spend a link code once; returns its tenant, or None if unknown/used/expired."""
    now = _now()
    cur = db.execute(
        "UPDATE telegram_link_codes SET used_at = ? "
        "WHERE code_hash = ? AND used_at IS NULL AND expires_at >= ?",
        (now, code_hash, now),
    )
    if cur.rowcount != 1:
        return None
    row = db.query_one(
        "SELECT tenant_id FROM telegram_link_codes WHERE code_hash = ?", (code_hash,)
    )
    return int(row["tenant_id"]) if row else None
