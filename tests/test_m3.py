"""M3 tests: gate decisions, gmail parsing, confirm gate routing, executor."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

from archon.db import repo
from archon.models import InboundMessage
from archon.pipeline import confirm
from archon.pipeline.ingest import decide
from archon.platforms.gmail.client import body_text, header, sender_address, sender_name
from archon.tools.registry import ToolContext

from test_m2 import make_rt  # reuse the runtime fixture


def _msg(**kw) -> InboundMessage:
    base = dict(platform="wa", source="wa", chat_id="c@g.us", chat_kind="group",
                msg_id="m1", sender_id="s", ts=datetime.now(UTC), text="hello")
    base.update(kw)
    return InboundMessage(**base)


def test_gate_fail_closed(tmp_path):
    rt = make_rt(tmp_path)
    # An unknown GROUP is still gated: groups stay whitelist-only for triage.
    assert decide(rt, None, _msg()) == (False, "not_whitelisted")
    # A whitelisted group passes.
    pk = repo.chat_upsert(rt.db, "wa", "c@g.us", "grp", "group")
    repo.chat_set_field(rt.db, pk, "is_whitelisted", 1)
    row = repo.chat_get(rt.db, "wa", "c@g.us")
    assert decide(rt, row, _msg()) == (True, "whitelisted")
    # monitor.groups=all triages an unknown group too.
    repo.setting_set(rt.db, "monitor.groups", "all")
    assert decide(rt, None, _msg()) == (True, "monitored_group")
    # Edits/deletes never reach triage (they go to the log-card path).
    assert decide(rt, None, _msg(is_edit=True)) == (False, "edit_or_delete_event")
    # Stale non-gmail messages are dropped; gmail is exempt (watermark guards it).
    old = _msg(ts=datetime.now(UTC) - timedelta(hours=7))
    assert decide(rt, None, old) == (False, "stale_message")
    # gmail default-on, switchable off.
    gm = _msg(platform="gmail", chat_id="a@b.com", chat_kind="email")
    assert decide(rt, None, gm)[0] is True
    repo.setting_set(rt.db, "gmail.triage_enabled", False)
    assert decide(rt, None, gm)[0] is False


def test_gate_monitors_private_chats_by_default(tmp_path):
    rt = make_rt(tmp_path)
    dm = _msg(chat_id="p@s.whatsapp.net", chat_kind="private")
    # Monitor-everything: an unknown DM is admitted by default...
    assert decide(rt, None, dm) == (True, "monitored_private")
    # ...and switchable back to the old whitelist-only behaviour.
    repo.setting_set(rt.db, "monitor.private_chats", "whitelist")
    assert decide(rt, None, dm) == (False, "not_whitelisted")


def test_gate_own_messages_are_triaged_unless_opted_out(tmp_path):
    rt = make_rt(tmp_path)
    own = _msg(chat_id="p@s.whatsapp.net", chat_kind="private", is_from_me=True)
    # Default: the owner's own message IS triaged (auto-reply skips it elsewhere).
    assert decide(rt, None, own) == (True, "monitored_private")
    repo.setting_set(rt.db, "monitor.include_own_messages", False)
    assert decide(rt, None, own) == (False, "from_me")


def test_gate_media_only_message_is_content_not_no_text(tmp_path):
    from archon.models import MediaRef

    rt = make_rt(tmp_path)
    # A caption-less photo in a whitelisted group must reach vision, not be
    # dropped as no_text before the image is ever looked at.
    pk = repo.chat_upsert(rt.db, "wa", "c@g.us", "grp", "group")
    repo.chat_set_field(rt.db, pk, "is_whitelisted", 1)
    row = repo.chat_get(rt.db, "wa", "c@g.us")
    photo = _msg(text=None, media=[MediaRef(kind="image", local_path=None)])
    assert decide(rt, row, photo) == (True, "whitelisted")
    # Truly empty (no text, no media) is still dropped.
    assert decide(rt, row, _msg(text=None)) == (False, "no_text")


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_gmail_parsing_helpers():
    msg = {
        "payload": {
            "headers": [
                {"name": "From", "value": '"Dana Cohen" <dana@example.com>'},
                {"name": "Subject", "value": "Meeting"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("hello world")},
                 "parts": []},
                {"mimeType": "text/html", "body": {"data": _b64("<b>x</b>")}, "parts": []},
            ],
            "body": {},
        }
    }
    assert header(msg, "subject") == "Meeting"
    assert sender_address(msg) == "dana@example.com"
    assert sender_name(msg) == "Dana Cohen"
    assert body_text(msg) == "hello world"


def test_confirm_flow_creates_row_and_executor_runs(tmp_path):
    rt = make_rt(tmp_path)
    ran: list[dict] = []

    async def fake_executor(rt_, payload, store=None):
        ran.append(payload)
        return "done"

    confirm.register_executor("test.kind", fake_executor)
    action_id = asyncio.run(confirm.request_confirmation(
        rt, kind="test.kind", payload={"a": 1}, description="d"))
    row = rt.db.query_one("SELECT * FROM pending_actions WHERE id = ?", (action_id,))
    assert row["status"] == "pending" and row["kind"] == "test.kind"
    # no control bot in tests → message not sent, row still exists
    result = asyncio.run(confirm._execute(rt, "test.kind", {"a": 1}))
    assert result == "done" and ran == [{"a": 1}]


def test_calendar_tool_routes_inbound_to_confirmation(tmp_path):
    from archon.tools import calendar as calendar_tools
    from archon.tools.registry import Registry

    rt = make_rt(tmp_path)
    registry = Registry()
    calendar_tools.register(registry)
    rt.registry = registry

    ctx = ToolContext(rt=rt, scope="inbound", origin_chat_pk=None,
                      extras={"source_msg_id": "x"})
    out = asyncio.run(registry.dispatch(ctx, "calendar_create_event", {
        "title": "Dentist", "start_iso": "2026-08-25T15:00:00",
    }))
    data = json.loads(out)
    assert data["status"] == "pending_owner_confirmation"
    row = rt.db.query_one("SELECT * FROM pending_actions WHERE id = ?",
                          (data["action_id"],))
    assert row is not None and row["kind"] == "event.create"
    payload = json.loads(row["payload_json"])
    assert payload["title"] == "Dentist"

    # owner-scope update/delete tools are hidden from inbound scope
    inbound_names = {s.name for s in registry.specs_for("inbound")}
    assert "calendar_delete_event" not in inbound_names
    assert "calendar_create_event" in inbound_names
