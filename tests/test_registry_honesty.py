"""Registry.dispatch surfaces what it used to hide: secret args stay out of the
audit, a dropped argument is reported, and a handler bug reaches the owner
instead of living only in a JSON string the model sees.
"""

from __future__ import annotations

import asyncio
import json

from archon.tools.registry import Registry, ToolContext

from test_m2 import make_rt


def _rt_with(tmp_path, tool_fn, name="t", sensitive=False, scopes=("owner",), schema=None):
    rt = make_rt(tmp_path)
    reg = Registry()
    reg.tool(name, "d", schema, scopes=scopes, sensitive=sensitive)(tool_fn)
    rt.registry = reg
    return rt, reg


def _audit(rt, action):
    from archon.db import repo
    return [r for r in repo.audit_query(rt.db, limit=50) if r["action"] == action]


def test_secret_args_are_redacted_in_the_audit(tmp_path):
    async def save_key(ctx, provider, api_key):
        return json.dumps({"ok": True})

    rt, reg = _rt_with(tmp_path, save_key, name="llm_set_key", sensitive=True, schema={
        "type": "object", "properties": {"provider": {"type": "string"},
                                          "api_key": {"type": "string"}}})
    ctx = ToolContext(rt=rt, scope="owner")
    asyncio.run(reg.dispatch(ctx, "llm_set_key", {"provider": "gemini", "api_key": "SECRET123"}))
    row = _audit(rt, "llm_set_key")[-1]
    detail = row["detail_json"]
    assert "SECRET123" not in detail
    assert "gemini" in detail and "•••" in detail


def test_an_unknown_argument_is_reported_not_dropped(tmp_path):
    seen = {}

    async def send(ctx, chat_id, text):
        seen["called"] = (chat_id, text)
        return json.dumps({"status": "sent"})

    rt, reg = _rt_with(tmp_path, send, name="tg_send", schema={
        "type": "object", "properties": {"chat_id": {"type": "string"},
                                          "text": {"type": "string"}}})
    ctx = ToolContext(rt=rt, scope="owner")
    out = asyncio.run(reg.dispatch(ctx, "tg_send",
                                   {"chat_id": "1", "text": "hi", "schedule": "later"}))
    data = json.loads(out)
    assert data["status"] == "sent"
    assert "schedule" in data["warning"]
    assert seen["called"] == ("1", "hi")  # the body still ran, minus the bad arg


def test_a_handler_bug_notes_tool_bug_sets_health_and_alerts(tmp_path):
    async def broken(ctx):
        raise AttributeError("'Runtime' object has no attribute 'query_one'")

    rt, reg = _rt_with(tmp_path, broken, name="calendar_list_events")
    ctx = ToolContext(rt=rt, scope="owner")
    out = asyncio.run(reg.dispatch(ctx, "calendar_list_events", {}))
    data = json.loads(out)
    assert data["is_error"] is True and "AttributeError" in data["error"]
    assert _audit(rt, "tool_bug")
    assert rt.health["tool:calendar_list_events"].startswith("bug: AttributeError")
    # No control bot -> the alert is queued, not lost.
    assert _audit(rt, "owner_alert_queued")


def test_a_user_error_goes_to_the_model_without_the_bug_path(tmp_path):
    async def raises_value(ctx):
        raise ValueError("unknown chat — use chat_find first")

    rt, reg = _rt_with(tmp_path, raises_value, name="whitelist_add")
    ctx = ToolContext(rt=rt, scope="owner")
    out = asyncio.run(reg.dispatch(ctx, "whitelist_add", {}))
    data = json.loads(out)
    assert data["is_error"] is True and "ValueError" in data["error"]
    assert _audit(rt, "tool_bug") == []  # a ValueError is user error, not a bug
    assert "tool:whitelist_add" not in rt.health
