"""The standard probe set: real inbound from the test account, asserted by
observed effect. Live-only — every act touches a real platform, so these run on
the VM behind ``probe.enabled``. The runner and waiters they use are unit-tested
offline; these wire them to the real transports.
"""

from __future__ import annotations

import time
from typing import Any

from ..runtime import Runtime
from . import session, waiters
from .runner import Probe, ProbeCtx, ProbeError


def _nonce() -> str:
    return f"probe{int(time.time())}"


# ---- Telegram (test account -> owner DM) -----------------------------------

def _tg_probes(rt: Runtime) -> list[Probe]:
    owner_id = rt.settings.telegram_owner_id
    if not owner_id:
        return []

    async def _dm(rt_: Runtime, text: str) -> Any:
        client = await session.test_client(rt_)
        return await client.send_message(int(owner_id), text)

    # 1) A plain DM must be ingested and cached (the gate + message cache).
    async def act_text(rt_: Runtime, ctx: ProbeCtx) -> None:
        ctx.scratch["nonce"] = n = _nonce()
        if rt_.settings.probe_dry_run:
            ctx.scratch["dry"] = True
            return
        await _dm(rt_, f"{n}: probe hello")

    async def expect_text(rt_: Runtime, ctx: ProbeCtx) -> str:
        if ctx.scratch.get("dry"):
            return "dry-run: not sent"
        row = await waiters.await_db_row(
            rt_, "SELECT id FROM messages WHERE text LIKE ?",
            (f"%{ctx.scratch['nonce']}%",), timeout_s=30)
        return f"message row id={row['id']} cached"

    # 2) A delete must produce a log-channel card read back through the userbot.
    async def act_delete(rt_: Runtime, ctx: ProbeCtx) -> None:
        ctx.scratch["nonce"] = n = _nonce()
        if rt_.settings.probe_dry_run:
            ctx.scratch["dry"] = True
            return
        client = await session.test_client(rt_)
        msg = await client.send_message(int(owner_id), f"{n}: delete me")
        # let the bot ingest it before the retraction
        await waiters.await_db_row(
            rt_, "SELECT id FROM messages WHERE text LIKE ?", (f"%{n}%",), timeout_s=30)
        await client.delete_messages(int(owner_id), [msg.id])

    async def expect_delete(rt_: Runtime, ctx: ProbeCtx) -> str:
        if ctx.scratch.get("dry"):
            return "dry-run: not sent"
        post = await waiters.await_log_channel_post(
            rt_, ctx.scratch["nonce"], timeout_s=45)
        return f"deletion card posted (msg id={getattr(post, 'id', '?')})"

    return [
        Probe("tg_text", "tg", act_text, expect_text, timeout_s=40),
        Probe("tg_delete", "tg", act_delete, expect_delete, timeout_s=90),
    ]


# ---- Gmail (owner mails themselves; include_self makes it inbound) ----------

def _gmail_probes(rt: Runtime) -> list[Probe]:
    async def act(rt_: Runtime, ctx: ProbeCtx) -> None:
        ctx.scratch["nonce"] = n = _nonce()
        if rt_.settings.probe_dry_run:
            ctx.scratch["dry"] = True
            return
        import asyncio as _a

        from ..platforms.gmail.client import GmailClient
        from ..platforms.google_auth import GoogleAuth
        gm = rt_.clients.get("gmail") or GmailClient(
            GoogleAuth(rt_.settings.google_token_path), rt=rt_)
        addr = await _a.to_thread(gm.get_profile_address)
        await _a.to_thread(gm.send, to=addr, subject=f"Archon probe {n}",
                           body=f"probe body {n}")

    async def expect(rt_: Runtime, ctx: ProbeCtx) -> str:
        if ctx.scratch.get("dry"):
            return "dry-run: not sent"
        row = await waiters.await_db_row(
            rt_, "SELECT id FROM messages WHERE platform='gmail' AND text LIKE ?",
            (f"%{ctx.scratch['nonce']}%",), timeout_s=120)
        return f"gmail inbound row id={row['id']}"

    return [Probe("gmail_self", "gmail", act, expect, timeout_s=150)]


# ---- WhatsApp (test number, behind probe.wa_enabled) -----------------------

def _wa_probes(rt: Runtime) -> list[Probe]:
    if not rt.settings.probe_wa_enabled:
        return []

    async def act(rt_: Runtime, ctx: ProbeCtx) -> None:
        # A second neonize session for the test number sends to the owner. That
        # session is paired once by phone code and lives at secrets/wa_test/.
        raise ProbeError("wa probe requires the paired test-number session (secrets/wa_test)")

    async def expect(rt_: Runtime, ctx: ProbeCtx) -> str:  # pragma: no cover - live only
        row = await waiters.await_db_row(
            rt_, "SELECT id FROM messages WHERE platform='wa' AND text LIKE ?",
            (f"%{ctx.scratch.get('nonce', '')}%",), timeout_s=60)
        return f"wa inbound row id={row['id']}"

    return [Probe("wa_text", "wa", act, expect, timeout_s=90)]


def build_probes(rt: Runtime) -> list[Probe]:
    return [*_tg_probes(rt), *_gmail_probes(rt), *_wa_probes(rt)]
