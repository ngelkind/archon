"""Input validation and honest results across the settings/logging/capture/
scheduling tools — each used to accept junk and report ok:true."""

from __future__ import annotations

import asyncio
import json

from archon.db import repo
from archon.tools import capture as capture_tools
from archon.tools import logging_ as logging_tools
from archon.tools import scheduling as scheduling_tools
from archon.tools import settings_ as settings_tools
from archon.tools.registry import Registry, ToolContext

from test_m2 import make_rt


def _reg(*mods):
    r = Registry()
    for m in mods:
        m.register(r)
    return r


def _rt(tmp_path, *mods):
    rt = make_rt(tmp_path)
    rt.registry = _reg(*mods)
    return rt


def _call(rt, name, args):
    return json.loads(asyncio.run(rt.registry.dispatch(ToolContext(rt=rt, scope="owner"), name, args)))


def test_settings_set_rejects_an_unknown_key_with_suggestions(tmp_path):
    rt = _rt(tmp_path, settings_tools)
    out = _call(rt, "settings_set", {"key": "gmail.triage_enabld", "value_json": "true"})
    assert "unknown setting key" in out["error"]
    assert "gmail.triage_enabled" in out["did_you_mean"]
    # And a known key still works.
    ok = _call(rt, "settings_set", {"key": "gmail.triage_enabled", "value_json": "false"})
    assert ok["ok"] and repo.setting_get(rt.db, "gmail.triage_enabled") is False


def test_settings_get_masks_secrets_on_the_single_key_path(tmp_path):
    rt = _rt(tmp_path, settings_tools)
    repo.setting_set(rt.db, "llm.key.gemini", "SECRET")
    out = _call(rt, "settings_get", {"key": "llm.key.gemini"})
    assert out["llm.key.gemini"] == "•••"


def test_chat_list_reports_total_and_shown(tmp_path):
    rt = _rt(tmp_path, settings_tools)
    for i in range(3):
        repo.chat_upsert(rt.db, "tg", f"-{i}", f"C{i}", "group")
    out = _call(rt, "chat_list", {"platform": "tg"})
    assert out["total"] == 3 and out["shown"] == 3 and len(out["chats"]) == 3
    assert "log_deletes" in out["chats"][0]


def test_capture_add_requires_an_existing_chat(tmp_path):
    rt = _rt(tmp_path, capture_tools)
    out = _call(rt, "capture_add", {"platform": "wa", "chat_id": "made-up@g.us"})
    assert "unknown chat" in out["error"]
    assert rt.db.query_one("SELECT 1 FROM chats WHERE chat_id='made-up@g.us'") is None
    # An existing chat arms fine.
    repo.chat_upsert(rt.db, "wa", "real@g.us", "R", "group")
    ok = _call(rt, "capture_add", {"platform": "wa", "chat_id": "real@g.us"})
    assert ok["ok"] and rt.db.query_one(
        "SELECT capture_media FROM chats WHERE chat_id='real@g.us'")[0] == 1


def test_log_channel_set_coerces_and_validates(tmp_path):
    rt = _rt(tmp_path, logging_tools)
    assert _call(rt, "log_channel_set", {"channel_id": -100999})["log_channel_id"] == -100999
    assert "numeric" in _call(rt, "log_channel_set", {"channel_id": "not-a-number"})["error"]
    assert _call(rt, "log_channel_set", {"channel_id": ""})["log_channel_id"] is None


def test_delay_policy_set_rejects_an_unknown_mode(tmp_path):
    rt = _rt(tmp_path, scheduling_tools)
    repo.chat_upsert(rt.db, "wa", "c@g.us", "C", "group")
    assert "none|fixed|random" in _call(
        rt, "delay_policy_set", {"platform": "wa", "chat_id": "c@g.us", "mode": "slow"})["error"]
    ok = _call(rt, "delay_policy_set",
               {"platform": "wa", "chat_id": "c@g.us", "mode": "fixed", "min_s": 60})
    assert ok["ok"]
