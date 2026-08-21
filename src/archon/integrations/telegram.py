"""Per-tenant Telegram linking via the product's Business bot.

The product model is not Google's. There is no per-user OAuth here: there is one
product bot, and each user connects **it** as their Telegram Business chatbot.
Telegram then delivers that user's private-chat messages tagged with a
``business_connection_id`` and lets the bot reply as them.

That gives us the routing key, but not the identity — a Business connection
update names a Telegram user, and Telegram has never heard of our accounts. So
linking is two hops:

1. The app asks for a **link code** (single-use, short-lived, stored hashed) and
   shows a ``t.me`` deep link.
2. The user opens it and the bot receives ``/start <code>``. Redeeming the code
   proves control of that Telegram account and binds ``tg_user_id -> tenant``.
3. Later, connecting the bot in Settings → Business → Chatbots produces a
   connection update whose user id resolves through that binding, and we store
   ``business_connection_id -> tenant``.

Compliance note: this is the official Bot API surface. It covers 1:1 private
chats only, has a 24-hour reply window, and (per the research) may require the
user to have Telegram Premium. Groups and history are NOT available this way —
that was the userbot's job in the single-user bot, and the userbot is a
ToS-grey path we deliberately do not extend to product tenants.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from ..db import repo
from ..db.tenancy import TenantScope

PROVIDER = "telegram"
_CODE_TTL_MINUTES = 15


class TelegramLinkError(RuntimeError):
    """The link flow could not be completed."""


def _hash(rt: Any, code: str) -> str:
    from ..api.security import hash_secret

    return hash_secret(rt.settings.api_token_pepper, code.strip().upper())


def _mint_code() -> str:
    """Short, unambiguous, and typed by a human into a chat.

    Excludes 0/O and 1/I; 8 chars from a 32-symbol alphabet is ~40 bits, which
    is far beyond guessable for a 15-minute single-use code.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def start_link(rt: Any, tenant_id: int) -> dict[str, Any]:
    """Issue a link code for a tenant; returns the code and a deep link."""
    code = _mint_code()
    expires = (datetime.now(UTC) + timedelta(minutes=_CODE_TTL_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    repo.telegram_link_code_create(rt.db, code_hash=_hash(rt, code),
                                   tenant_id=tenant_id, expires_at=expires)
    username = (rt.settings.telegram_bot_username or "").lstrip("@")
    deep_link = f"https://t.me/{username}?start={code}" if username else None
    return {"code": code, "deep_link": deep_link,
            "expires_in_minutes": _CODE_TTL_MINUTES}


def complete_link(rt: Any, *, code: str, tg_user_id: str,
                  tg_username: str | None = None,
                  tg_name: str | None = None) -> int:
    """Redeem a code from a Telegram user; returns the tenant now linked.

    The Telegram identity comes from the update Telegram delivered, never from
    anything the user typed — the only thing they supply is the code.
    """
    tenant_id = repo.telegram_link_code_consume(rt.db, _hash(rt, code))
    if tenant_id is None:
        raise TelegramLinkError("invalid, expired, or already-used link code")
    scope = TenantScope(rt.db, tenant_id)
    repo.telegram_link_upsert(scope, tg_user_id=str(tg_user_id),
                              tg_username=tg_username, tg_name=tg_name)
    rt.audit.note("telegram_linked", tenant_id=tenant_id, tg_user_id=str(tg_user_id))
    return tenant_id


def record_connection(rt: Any, *, tg_user_id: str, business_connection_id: str,
                      enabled: bool) -> int | None:
    """Handle a Business connection update; returns the tenant, or None if the
    Telegram account was never linked to one."""
    tenant_id = repo.telegram_tenant_by_user_id(rt.db, str(tg_user_id))
    if tenant_id is None:
        rt.audit.note("telegram_connection_unlinked_user",
                      tg_user_id=str(tg_user_id))
        return None
    repo.telegram_link_set_connection(
        TenantScope(rt.db, tenant_id),
        business_connection_id=business_connection_id if enabled else None,
        enabled=enabled,
    )
    rt.audit.note("telegram_business_connection", tenant_id=tenant_id,
                  enabled=bool(enabled))
    return tenant_id


def tenant_for_connection(rt: Any, business_connection_id: str | None) -> int | None:
    """Route an inbound business message to its tenant."""
    if not business_connection_id:
        return None
    return repo.telegram_tenant_by_connection(rt.db, business_connection_id)


def tenant_for_user(rt: Any, tg_user_id: str | int) -> int | None:
    """Route a plain DM to the product bot (the Bot API front door)."""
    return repo.telegram_tenant_by_user_id(rt.db, str(tg_user_id))


def connection_id_for(store: Any) -> str | None:
    """This tenant's Business connection id, for sending as them."""
    row = repo.telegram_link_get(store)
    if row is None or not row["is_enabled"]:
        return None
    return row["business_connection_id"]


def status(store: Any) -> dict[str, Any]:
    row = repo.telegram_link_get(store)
    if row is None:
        return {"linked": False, "connected": False}
    return {
        "linked": True,
        "connected": bool(row["is_enabled"] and row["business_connection_id"]),
        "tg_username": row["tg_username"],
        "tg_name": row["tg_name"],
        "linked_at": row["linked_at"],
        "connected_at": row["connected_at"],
    }


def unlink(rt: Any, tenant_id: int) -> bool:
    revoked = repo.telegram_link_revoke(TenantScope(rt.db, tenant_id))
    if revoked:
        rt.audit.note("telegram_unlinked", tenant_id=tenant_id)
    return revoked
