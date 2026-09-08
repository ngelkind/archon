"""Drain the log_outbox: render, coalesce, digest bursts, retry, record.

A supervised subsystem. tglog.log_change only enqueues; this loop owns the
network send, so the ingest consumer never blocks on a rate-limited card. It:

* waits out each card's coalesce window (edits of one message merged upstream);
* when one chat has more than ``DIGEST_THRESHOLD`` cards waiting, posts a single
  digest ("14 edits/deletes in <chat>") instead of that many messages, staying
  under Telegram's ~20-messages/min-per-channel flood limit;
* retries via throttled_send (which rebuilds a dead notifier), and drops a card
  only after ``MAX_ATTEMPTS`` so one unroutable card cannot wedge the queue.
"""

from __future__ import annotations

import asyncio

from ..db import repo
from ..runtime import Runtime
from . import tglog

TICK_S = 2.0
DIGEST_THRESHOLD = 10
MAX_ATTEMPTS = 6
_BATCH = 50


async def _post(rt: Runtime, channel: int, text: str) -> bool:
    from .send import throttled_send

    result = await throttled_send(rt, lambda b: b.send_message(channel, text),
                                  kind="log_card")
    return result is not None


async def _drain_once(rt: Runtime) -> int:
    """One pass. Returns how many outbox rows were resolved (sent or dropped)."""
    now = repo._now()
    due = repo.log_outbox_due(rt.db, now, limit=_BATCH)
    if not due:
        return 0
    channel = tglog.channel_id(rt)
    if channel is None:
        # No channel: leave the rows pending (they are the record) but say so.
        rt.audit.note("tglog_no_channel_worker", pending=len(due))
        return 0
    resolved = 0

    # Group by chat so a burst becomes one digest.
    by_chat: dict[tuple, list] = {}
    for row in due:
        by_chat.setdefault((row["tenant_id"], row["platform"], row["chat_id"]), []).append(row)

    for (tenant_id, platform, chat_id), rows in by_chat.items():
        if len(rows) > DIGEST_THRESHOLD:
            label = rows[0]["chat_label"] or chat_id
            edits = sum(1 for r in rows if r["kind"] == "edited")
            dels = len(rows) - edits
            digest = (f"\U0001f4dd <b>{len(rows)} message changes</b> in "
                      f"{label} — {edits} edited, {dels} deleted (burst digested).")
            ok = await _post(rt, channel, digest)
            ids = [int(r["id"]) for r in rows]
            if ok:
                repo.log_outbox_mark_sent(rt.db, ids)
                rt.audit.note("tglog_digest_sent", tenant_id=tenant_id, chat=chat_id,
                              count=len(rows))
                resolved += len(rows)
            else:
                repo.log_outbox_mark_failed(rt.db, ids, "digest send failed")
            continue
        for row in rows:
            card = tglog.render_card(
                rt, kind=row["kind"], platform=platform,
                chat_label=row["chat_label"] or chat_id, sender=row["sender"] or "unknown",
                before=row["before_text"], after=row["after_text"])
            if card is None:  # redaction failed -> suppress, do not post cleartext
                repo.log_outbox_mark_sent(rt.db, [int(row["id"])])
                rt.audit.note("tglog_suppressed_redaction", tenant_id=tenant_id, chat=chat_id)
                resolved += 1
                continue
            ok = await _post(rt, channel, card)
            if ok:
                repo.log_outbox_mark_sent(rt.db, [int(row["id"])])
                rt.audit.note("tglog_sent", tenant_id=tenant_id, platform=platform,
                              chat=chat_id, kind=row["kind"], outbox_id=int(row["id"]))
                resolved += 1
            else:
                repo.log_outbox_mark_failed(rt.db, [int(row["id"])], "send failed")
                rt.audit.note("tglog_failed", tenant_id=tenant_id, chat=chat_id,
                              outbox_id=int(row["id"]), attempts=int(row["attempts"]) + 1)

    dropped = repo.log_outbox_drop_exhausted(rt.db, MAX_ATTEMPTS)
    if dropped:
        rt.audit.note("tglog_dropped_exhausted", count=dropped)
        resolved += dropped
    return resolved


async def run(rt: Runtime) -> None:
    rt.health["logworker"] = "running"
    while True:
        try:
            await _drain_once(rt)
        except Exception as exc:  # noqa: BLE001 — one bad pass must not kill the drain
            rt.audit.note("logworker_error", error=repr(exc)[:200])
        await asyncio.sleep(TICK_S)
