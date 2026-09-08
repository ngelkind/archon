"""Expired confirmations are swept, and a delayed reply that fails keeps its
draft as 'failed' instead of vanishing as 'cancelled'."""

from __future__ import annotations

import pytest

from archon.db import repo
from archon.pipeline import confirm
from archon.scheduler import loop as scheduler_loop
from archon.testing.harness import Harness

from conftest import run_async
from test_m2 import make_rt

pytestmark = pytest.mark.e2e


@run_async
async def test_overdue_approvals_are_swept_and_publish_expired(tmp_path, monkeypatch):
    rt = make_rt(tmp_path)
    # Two pending actions: one already overdue, one not.
    old = repo.pending_action_create(rt.db, kind="event.create", payload_json="{}",
                                     chat_pk=None, expires_at="2000-01-01 00:00:00")
    fresh = repo.pending_action_create(rt.db, kind="event.create", payload_json="{}",
                                       chat_pk=None, expires_at="2099-01-01 00:00:00")
    events = []
    monkeypatch.setattr(rt.events, "publish",
                        lambda kind, **d: events.append((kind, d)))
    await scheduler_loop._expire_stale_approvals(rt)
    assert rt.db.query_one("SELECT status FROM pending_actions WHERE id=?", (old,))["status"] == "expired"
    assert rt.db.query_one("SELECT status FROM pending_actions WHERE id=?", (fresh,))["status"] == "pending"
    assert ("approval.resolved", {"action_id": old, "action_kind": "event.create",
                                  "status": "expired", "actor": "sweep"}) in events
    assert any(r["action"] == "confirm_expired" for r in repo.audit_query(rt.db, limit=10))


@run_async
async def test_the_scheduler_tick_runs_the_sweep(tmp_path):
    async with await Harness.start(tmp_path, subsystems=("scheduler",)) as h:
        repo.pending_action_create(h.rt.db, kind="event.create", payload_json="{}",
                                   chat_pk=None, expires_at="2000-01-01 00:00:00")
        await h.wait_for_audit("confirm_expired")


def test_a_failed_delayed_reply_is_marked_failed_not_cancelled(tmp_path):
    import asyncio

    rt = make_rt(tmp_path)
    pk = repo.chat_upsert(rt.db, "wa", "x@g.us", "G", "group")
    repo.pending_reply_create(rt.db, chat_pk=pk, draft_text="hello there",
                              due_at="2000-01-01 00:00:00")

    async def boom(rt_, kind, payload, store):
        raise RuntimeError("send failed")

    from archon.scheduler import loop as sl
    sl_execute = sl._execute
    try:
        import archon.scheduler.loop as loopmod
        loopmod._execute = boom  # type: ignore[assignment]
        asyncio.run(sl._fire_pending_replies(rt))
    finally:
        loopmod._execute = sl_execute  # type: ignore[assignment]
    row = rt.db.query_one("SELECT status, draft_text FROM pending_replies")
    assert row["status"] == "failed" and row["draft_text"] == "hello there"
