"""M8/M9 tests: delay policies, scheduler firing, personas, sub-bot filtering."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from archon.db import repo
from archon.pipeline import confirm
from archon.platforms.telegram.subbots import FilteredRegistry
from archon.scheduler.delays import compute_due, parse_policy
from archon.scheduler.loop import _fire_pending_replies, _fire_scheduled
from archon.tools.registry import Registry, Tool, ToolContext

from test_m2 import make_rt


def test_delay_policies():
    assert compute_due(None) is None
    assert compute_due('{"mode": "none"}') is None
    assert compute_due("not json") is None

    now = datetime.now(UTC)
    fixed = compute_due(json.dumps({"mode": "fixed", "min_s": 300}), now)
    assert fixed == now + timedelta(seconds=300)

    rand = compute_due(json.dumps({"mode": "random", "min_s": 3600, "max_s": 18000}), now)
    assert now + timedelta(seconds=3600) <= rand <= now + timedelta(seconds=18000)

    assert parse_policy('{"mode": "bogus"}') == {"mode": "none"}


def test_scheduler_fires_due_messages(tmp_path):
    rt = make_rt(tmp_path)
    sent: list[dict] = []

    async def fake_wa_send(rt_, payload):
        sent.append(payload)
        return "ok"

    confirm.register_executor("wa.send", fake_wa_send)
    chat_pk = repo.chat_upsert(rt.db, "wa", "123@s.whatsapp.net", "Test", "private")
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    rt.db.execute("INSERT INTO scheduled_messages (platform, chat_pk, text, due_at) "
                  "VALUES ('wa', ?, 'due now', ?)", (chat_pk, past))
    rt.db.execute("INSERT INTO scheduled_messages (platform, chat_pk, text, due_at) "
                  "VALUES ('wa', ?, 'later', ?)", (chat_pk, future))

    asyncio.run(_fire_scheduled(rt))
    assert len(sent) == 1 and sent[0]["text"] == "due now"
    rows = {r["text"]: r["status"] for r in rt.db.query("SELECT * FROM scheduled_messages")}
    assert rows["due now"] == "sent" and rows["later"] == "pending"


def test_pending_reply_fires(tmp_path):
    rt = make_rt(tmp_path)
    sent = []

    async def fake_send(rt_, payload):
        sent.append(payload)
        return "ok"

    confirm.register_executor("wa.send", fake_send)
    chat_pk = repo.chat_upsert(rt.db, "wa", "9@s.whatsapp.net", "P", "private")
    past = (datetime.now(UTC) - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    rt.db.execute("INSERT INTO pending_replies (chat_pk, draft_text, due_at) "
                  "VALUES (?, 'delayed hi', ?)", (chat_pk, past))
    asyncio.run(_fire_pending_replies(rt))
    assert sent and sent[0]["text"] == "delayed hi"


def test_filtered_registry_scoping(tmp_path):
    rt = make_rt(tmp_path)
    registry = Registry()

    async def h(ctx, **kw):
        return "ok"

    for name in ("wa_send_message", "tg_send_private", "email_send", "chat_list",
                 "llm_set_provider"):
        registry.add(Tool(name=name, description="", handler=h,
                          input_schema={"type": "object", "properties": {}},
                          scopes=frozenset({"owner"})))

    wa_view = FilteredRegistry(registry, "wa")
    names = {s.name for s in wa_view.specs_for("owner")}
    assert "wa_send_message" in names and "chat_list" in names
    assert "tg_send_private" not in names and "llm_set_provider" not in names

    ctx = ToolContext(rt=rt, scope="owner")
    out = asyncio.run(wa_view.dispatch(ctx, "llm_set_provider", {}))
    assert "not available" in out


def test_persona_roundtrip(tmp_path):
    from archon.tools import contexts as context_tools

    rt = make_rt(tmp_path)
    registry = Registry()
    context_tools.register(registry)
    rt.registry = registry
    ctx = ToolContext(rt=rt, scope="owner")

    out = asyncio.run(registry.dispatch(ctx, "persona_create", {
        "name": "negotiator",
        "system_prompt": "Be firm but polite; anchor high.",
    }))
    assert json.loads(out)["ok"]

    repo.chat_upsert(rt.db, "wa", "client@s.whatsapp.net", "Client X", "private")
    out = asyncio.run(registry.dispatch(ctx, "persona_assign", {
        "platform": "wa", "chat_id": "client@s.whatsapp.net",
        "persona_name": "negotiator",
    }))
    assert json.loads(out)["persona"] == "negotiator"
    row = repo.chat_get(rt.db, "wa", "client@s.whatsapp.net")
    persona = rt.db.query_one("SELECT * FROM personas WHERE id = ?", (row["persona_id"],))
    assert persona["name"] == "negotiator"


def test_download_command_parser():
    from archon.platforms.telegram.download_cmd import is_download_command
    assert is_download_command("/download https://youtu.be/abc") == "https://youtu.be/abc"
    assert is_download_command("/download  https://tiktok.com/@x/video/1 ") == "https://tiktok.com/@x/video/1"
    assert is_download_command("/download no url here") is None
    assert is_download_command("just text") is None
    assert is_download_command("/downloadfoo") is None
    assert is_download_command(None) is None
