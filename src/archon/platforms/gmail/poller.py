"""Gmail poll loop → InboundMessage on the bus.

Polls the inbox on an interval (no inbound ports, no push endpoint). Dedupe
relies on the messages-table unique index, plus a persisted watermark in
gmail_state so restarts don't replay history.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ...db.repo import setting_get  # noqa: F401  (imported for parity; watermark uses gmail_state)
from ...models import InboundMessage
from ...runtime import Runtime
from .client import GmailClient, body_text, header, sender_address, sender_name


def _state_get(rt: Runtime, key: str) -> str | None:
    row = rt.db.query_one("SELECT value FROM gmail_state WHERE key = ?", (key,))
    return row["value"] if row else None


def _state_set(rt: Runtime, key: str, value: str) -> None:
    rt.db.execute(
        "INSERT INTO gmail_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _to_inbound(msg: dict) -> InboundMessage:
    internal_ms = int(msg.get("internalDate", "0"))
    ts = datetime.fromtimestamp(internal_ms / 1000, tz=UTC) if internal_ms else datetime.now(UTC)
    addr = sender_address(msg)
    subject = header(msg, "Subject")
    text = body_text(msg)
    return InboundMessage(
        platform="gmail",
        source="gmail",
        chat_id=addr,                      # one "chat" per correspondent address
        chat_kind="email",
        chat_name=addr,
        msg_id=str(msg["id"]),
        sender_id=addr,
        sender_name=sender_name(msg),
        ts=ts,
        text=f"Subject: {subject}\n\n{text}" if subject else text,
        raw={"threadId": msg.get("threadId"),
             "message_id_header": header(msg, "Message-ID"),
             "subject": subject},
    )


async def run(rt: Runtime) -> None:
    from ..google_auth import GoogleAuth

    auth = GoogleAuth(rt.settings.google_token_path)
    client = GmailClient(auth)
    rt.clients["gmail"] = client
    rt.health["gmail"] = "polling"

    if _state_get(rt, "bootstrapped") is None:
        # First run: mark the current time; only mail arriving after Archon
        # started is processed — no replaying years of inbox.
        _state_set(rt, "bootstrapped", datetime.now(UTC).isoformat())

    while True:
        try:
            stubs = await asyncio.to_thread(
                client.list_messages, query="in:inbox -from:me newer_than:1d", limit=25
            )
            bootstrap = datetime.fromisoformat(_state_get(rt, "bootstrapped"))  # type: ignore[arg-type]
            for stub in stubs:
                seen = rt.db.query_one(
                    "SELECT 1 FROM messages WHERE platform = 'gmail' AND msg_id = ?",
                    (stub["id"],),
                )
                if seen:
                    continue
                full = await asyncio.to_thread(client.get_message, stub["id"])
                inbound = _to_inbound(full)
                if inbound.ts < bootstrap:
                    continue
                await rt.bus.publish(inbound)
            rt.health["gmail"] = "polling"
        except Exception as exc:  # noqa: BLE001 — poll loop must survive
            rt.health["gmail"] = f"error: {type(exc).__name__}"
            rt.audit.note("gmail_poll_error", error=repr(exc)[:300])
        await asyncio.sleep(rt.settings.gmail_poll_seconds)
