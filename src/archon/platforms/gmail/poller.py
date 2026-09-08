"""Gmail poll loop → InboundMessage on the bus.

Polls the inbox on an interval (no inbound ports, no push endpoint). Dedupe
relies on the messages-table unique index, plus a persisted watermark in
gmail_state so restarts don't replay history.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ...db import repo
from ...models import InboundMessage
from ...runtime import Runtime
from .client import GmailClient, body_text, header, sender_address, sender_name


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


def polling_tenants(rt: Runtime) -> list[int]:
    """Every tenant whose Gmail should be polled this round.

    The owner is included whenever their file token exists, so the personal bot
    keeps polling exactly as before without having to link through OAuth. Every
    other tenant appears only once they have linked a Google account.
    """
    from ...db.tenancy import OWNER_TENANT_ID

    linked = repo.integration_linked_tenants(rt.db, "google")
    if OWNER_TENANT_ID not in linked and rt.settings.google_token_path.exists():
        linked.insert(0, OWNER_TENANT_ID)
    return linked


# TODO(TOS-REVIEW): Gmail/Google — polls and ingests a user's full inbox history — restricted-scope Gmail data under Limited Use — review before launch
async def _poll_tenant(rt: Runtime, tenant_id: int) -> int:
    """One tenant's inbox: their credentials, their watermark, their messages.

    Returns how many messages were published. Each inbound carries
    ``tenant_id``, so the gate, triage, agent and memory downstream all run
    under that tenant — nothing here reaches another tenant's data.
    """
    from ...db.tenancy import TenantScope
    from ...integrations import google as google_integration

    store = TenantScope(rt.db, tenant_id)
    auth = google_integration.auth_for(rt, tenant_id)
    client = GmailClient(auth, rt=rt)
    if tenant_id == _owner_id():
        # The single-user bot's out-of-band sends still expect this handle.
        rt.clients["gmail"] = client

    if repo.gmail_state_get(store, "bootstrapped") is None:
        # First run for THIS tenant: only mail arriving after they linked is
        # processed — no replaying years of someone's inbox on connect.
        repo.gmail_state_set(store, "bootstrapped", datetime.now(UTC).isoformat())

    stubs = await asyncio.to_thread(
        client.list_messages, query="in:inbox -from:me newer_than:1d", limit=25
    )
    bootstrap = datetime.fromisoformat(
        repo.gmail_state_get(store, "bootstrapped")  # type: ignore[arg-type]
    )
    published = 0
    for stub in stubs:
        if repo.message_exists(store, "gmail", stub["id"]):
            continue
        full = await asyncio.to_thread(client.get_message, stub["id"])
        inbound = _to_inbound(full)
        if inbound.ts < bootstrap:
            continue
        inbound.tenant_id = tenant_id
        await rt.bus.publish(inbound)
        published += 1
    repo.gmail_state_set(store, "last_poll_at", datetime.now(UTC).isoformat())
    return published


def _owner_id() -> int:
    from ...db.tenancy import OWNER_TENANT_ID

    return OWNER_TENANT_ID


async def run(rt: Runtime) -> None:
    rt.health["gmail"] = "polling"
    while True:
        tenants = polling_tenants(rt)
        failures = 0
        for tenant_id in tenants:
            try:
                await _poll_tenant(rt, tenant_id)
            except Exception as exc:  # noqa: BLE001 — one tenant must not stop the rest
                failures += 1
                rt.audit.note("gmail_poll_error", tenant_id=tenant_id,
                              error=repr(exc)[:300])
        rt.health["gmail"] = (
            f"polling {len(tenants)} tenant(s)" if not failures
            else f"polling {len(tenants)} tenant(s), {failures} failing"
        )
        await asyncio.sleep(rt.settings.gmail_poll_seconds)
