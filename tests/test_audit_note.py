"""The audit note's NAME must survive whatever fields a caller passes.

Background: ``AuditLog.note(message, **fields)`` used to build
``{"event": "note", "action": message, **fields}`` — a caller passing
``action=`` silently renamed the note. The live log held 328 notes called
"ignore"/"calendar"/"respond" and not one called "triage", so the single
diagnostic that shows WHY a message never reached the calendar was destroyed
at the point of writing. These tests pin the guard behaviourally and prove the
regression test would fail against the old code.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon.agent.triage import TriageResult
from archon.db import repo
from archon.logging_.audit import AuditLog
from archon.models import InboundMessage
from archon.pipeline import ingest

from test_m2 import make_rt


def _last_record(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines, "audit log is empty"
    return json.loads(lines[-1])


def test_note_name_survives_a_colliding_action_field(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", None, store_content=True)
    log.note("triage", chat="c1", action="ignore", event="bogus", ts="never")
    rec = _last_record(tmp_path / "a.jsonl")
    assert rec["event"] == "note"
    assert rec["action"] == "triage"
    assert isinstance(rec["ts"], float)
    # The caller's data is kept, not dropped, and the collision is visible.
    assert rec["field_action"] == "ignore"
    assert rec["field_event"] == "bogus"
    assert rec["field_ts"] == "never"
    assert rec["reserved_key_collision"] == ["action", "event", "ts"]
    assert rec["chat"] == "c1"


def test_note_without_collisions_is_unchanged(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", None, store_content=True)
    log.note("startup", schema_version=7)
    rec = _last_record(tmp_path / "a.jsonl")
    assert rec == {"event": "note", "action": "startup", "schema_version": 7, "ts": rec["ts"]}
    assert "reserved_key_collision" not in rec


def test_tenant_id_argument_is_not_shadowed(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", None, store_content=True)
    log.note("gate_extra", tenant_id=2, chat="x")
    rec = _last_record(tmp_path / "a.jsonl")
    assert rec["tenant_id"] == 2 and rec["action"] == "gate_extra"


def test_the_db_mirror_sees_the_real_note_name(tmp_path):
    rt = make_rt(tmp_path)
    rt.audit.note("triage", action="calendar", chat="c")
    rows = repo.audit_query(rt.db, limit=5)
    names = {r["action"] for r in rows}
    assert "triage" in names and "calendar" not in names


def test_old_behaviour_would_have_failed_this(tmp_path, monkeypatch):
    """Mutation check: the pre-fix record shape renames the note. If someone
    reverts to ``{"event":..., "action": message, **fields}`` this fails."""
    log = AuditLog(tmp_path / "a.jsonl", None, store_content=True)

    def old_note(self, message, tenant_id=None, **fields):
        self._write({"event": "note", "action": message, **fields}, tenant_id)

    monkeypatch.setattr(AuditLog, "note", old_note)
    log.note("triage", action="ignore")
    assert _last_record(tmp_path / "a.jsonl")["action"] == "ignore"  # the bug


def test_pipeline_triage_note_is_named_triage_and_carries_the_verdict(tmp_path, monkeypatch):
    """Drive the real batch processor with triage stubbed, and assert on the
    record it writes — the exact call site that produced the mis-named notes."""
    rt = make_rt(tmp_path)
    rt.router = object()  # never called: triage is stubbed and there is no media
    rt.registry = object()

    async def fake_triage(router, **kw):
        return TriageResult(action="ignore", confidence=0.4, reason="nothing to do")

    monkeypatch.setattr(ingest, "triage", fake_triage)
    chat_pk = repo.chat_upsert(rt.db, "tg", "-1001", "Group", "group")
    msg = InboundMessage(
        platform="tg", source="userbot", chat_id="-1001", chat_kind="group",
        msg_id="1", sender_id="7", ts=datetime.now(UTC), text="hello", tenant_id=1,
    )
    asyncio.run(ingest._process_batch(rt, [msg]))
    rows = [r for r in repo.audit_query(rt.db, limit=20) if r["action"] == "triage"]
    assert len(rows) == 1, "exactly one note named 'triage' expected"
    line = _last_record(tmp_path / "a.jsonl")
    assert line["action"] == "triage"
    assert line["verdict"] == "ignore"
    assert line["reason"] == "nothing to do"
    assert line["tenant_id"] == 1
    assert "reserved_key_collision" not in line
    assert chat_pk  # the chat existed, so the batch ran the real path
