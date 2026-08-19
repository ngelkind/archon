"""M1 smoke tests: schema, migrations, repo accessors, audit."""

from __future__ import annotations

from datetime import UTC, datetime

from archon.db import Db
from archon.db.migrations import migrate
from archon.db import repo
from archon.logging_.audit import AuditLog
from archon.models import InboundMessage


def _mem_db(tmp_path):
    db = Db(tmp_path / "test.db")
    assert migrate(db) >= 1
    return db


def test_migrate_idempotent(tmp_path):
    db = _mem_db(tmp_path)
    v1 = migrate(db)
    v2 = migrate(db)
    assert v1 == v2


def test_settings_roundtrip(tmp_path):
    db = _mem_db(tmp_path)
    assert repo.setting_get(db, "x", "fallback") == "fallback"
    repo.setting_set(db, "x", {"a": 1})
    assert repo.setting_get(db, "x") == {"a": 1}
    repo.setting_set(db, "x", [1, 2])
    assert repo.setting_get(db, "x") == [1, 2]


def test_chat_and_message_cache(tmp_path):
    db = _mem_db(tmp_path)
    pk = repo.chat_upsert(db, "wa", "123@g.us", "Family", "group")
    assert repo.chat_upsert(db, "wa", "123@g.us", None, "group") == pk  # name kept
    assert repo.chat_get(db, "wa", "123@g.us")["name"] == "Family"

    msg = InboundMessage(
        platform="wa", source="wa", chat_id="123@g.us", chat_kind="group",
        msg_id="m1", sender_id="s1", ts=datetime.now(UTC), text="hello",
    )
    repo.message_upsert(db, msg, pk)
    repo.message_upsert(db, msg, pk)  # duplicate delivery is a no-op
    before = repo.message_mark_edited(db, "wa", "123@g.us", "m1", "hello edited")
    assert before["text"] == "hello"
    before_del = repo.message_mark_deleted(db, "wa", "123@g.us", "m1")
    assert before_del["edited_text"] == "hello edited"


def test_llm_cost_tracking(tmp_path):
    db = _mem_db(tmp_path)
    repo.llm_call_record(
        db, purpose="triage", provider="gemini", model="flash",
        in_tokens=100, out_tokens=20, cost_usd=0.0001,
    )
    day = repo.llm_cost_since(db, "-1 day")
    assert day["calls"] == 1 and day["cost"] > 0
    rows = repo.llm_cost_breakdown(db, "-1 day")
    assert rows[0]["provider"] == "gemini"


def test_audit_content_policy(tmp_path):
    db = _mem_db(tmp_path)
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path, db, store_content=False)
    audit.gate(platform="wa", chat_id="c", sender_id="s", allowed=True,
               reason="whitelisted", text="SECRET")
    audit.gate(platform="wa", chat_id="c2", sender_id="s", allowed=False,
               reason="not_whitelisted", text="ALSO SECRET")
    content = log_path.read_text(encoding="utf-8")
    assert "SECRET" not in content  # store_content=False → never stored
    rows = db.query("SELECT * FROM audit")
    assert len(rows) == 2
