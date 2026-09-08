"""Waiters: poll for an OBSERVED effect until a deadline, then give up.

Every probe assertion goes through one of these. They read the same evidence an
operator would — the audit log, the DB, the Calendar API, the log channel — so a
green probe means the effect really happened, not that a call returned 200.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..runtime import Runtime
from .runner import ProbeCtx, ProbeError


async def _poll(check: Any, *, timeout_s: float, what: str, interval: float = 0.3) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        got = await check()
        if got is not None:
            return got
        if time.monotonic() >= deadline:
            raise ProbeError(f"timed out after {timeout_s:.0f}s waiting for {what}")
        await asyncio.sleep(interval)


def _read_audit(rt: Runtime) -> list[dict[str, Any]]:
    path = rt.audit.path
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


async def await_audit(rt: Runtime, ctx: ProbeCtx, action: str, *,
                      timeout_s: float = 30.0, **match: Any) -> dict[str, Any]:
    """Wait for an audit note ``action`` written AFTER this probe's baseline,
    with every ``match`` field equal (string-compared)."""
    async def check() -> dict[str, Any] | None:
        for rec in reversed(_read_audit(rt)[ctx.after_line:]):
            if rec.get("action") != action:
                continue
            if all(str(rec.get(k)) == str(v) for k, v in match.items()):
                return rec
        return None
    return await _poll(check, timeout_s=timeout_s,
                       what=f"audit {action} {match or ''}".strip())


async def await_db_row(rt: Runtime, sql: str, params: tuple = (), *,
                       timeout_s: float = 30.0) -> dict[str, Any]:
    async def check() -> Any:
        return rt.db.query_one(sql, params)
    return await _poll(check, timeout_s=timeout_s, what=f"db row ({sql[:60]})")


async def await_calendar_event(rt: Runtime, title_contains: str, *,
                               timeout_s: float = 45.0) -> dict[str, Any]:
    """Poll the Calendar API for an event whose title contains ``title_contains``
    (the real proof a calendar_create_event actually landed in Google)."""
    cal = rt.clients.get("calendar")
    if cal is None:
        raise ProbeError("no calendar client connected")

    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    tmin = (now - timedelta(hours=1)).isoformat()
    tmax = (now + timedelta(days=14)).isoformat()

    async def check() -> Any:
        events = await asyncio.to_thread(
            cal.list_events, calendar_id="primary", time_min_iso=tmin,
            time_max_iso=tmax, query=title_contains, limit=25)
        for ev in events or []:
            if title_contains.lower() in (ev.get("summary") or "").lower():
                return ev
        return None
    return await _poll(check, timeout_s=timeout_s,
                       what=f"calendar event ~{title_contains!r}")


async def await_log_channel_post(rt: Runtime, contains: str, *,
                                 timeout_s: float = 40.0) -> Any:
    """Read the log channel back through the OWNER's userbot (which is a member)
    and wait for a post containing ``contains`` — the deletion/edit card proof."""
    from ..logging_.capture import _log_channel
    client = rt.clients.get("tg_userbot")
    if client is None:
        raise ProbeError("no telegram userbot connected to read the log channel")
    channel = _log_channel(rt)
    if not channel:
        raise ProbeError("no log channel configured")

    async def check() -> Any:
        msgs = await client.get_messages(int(channel), limit=20)
        for m in msgs or []:
            if contains.lower() in (getattr(m, "text", "") or "").lower():
                return m
        return None
    return await _poll(check, timeout_s=timeout_s, what=f"log-channel post ~{contains!r}")
