"""Control API (Phase 0) tests: owner-turn parity + streaming events, generic
tool listing, device-token auth, and code-based pairing. No network: the agent
is stubbed or driven by a fake provider."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from archon.agent.agent import ToolCallEvent, ToolResultEvent, run_agent
from archon.api.security import hash_secret, mint_token
from archon.api.server import build_app
from archon.bus import Bus
from archon.config import Settings
from archon.db import Db, repo
from archon.db.migrations import migrate
from archon.llm.base import ChatMessage, LLMResult, ProviderError, ToolCall, Usage
from archon.llm.router import Router
from archon.logging_.audit import AuditLog
from archon.runtime import Runtime
from archon.tools import system as system_tools
from archon.tools.registry import Registry, Tool, ToolContext


def make_rt(tmp_path) -> Runtime:
    db = Db(tmp_path / "t.db")
    migrate(db)
    settings = Settings(
        telegram_bot_token="x", telegram_owner_id=1,
        archon_data=tmp_path, archon_secrets=tmp_path,
        llm_active_provider="gemini", gemini_api_key="fake",
        api_token_pepper="test-pepper",
        _env_file=None,
    )
    audit = AuditLog(tmp_path / "a.jsonl", db, store_content=True)
    rt = Runtime(settings=settings, db=db, audit=audit, bus=Bus())
    rt.router = Router(rt)
    registry = Registry()
    system_tools.register(registry)
    rt.registry = registry
    return rt


def _pepper(rt: Runtime) -> str:
    return rt.settings.api_token_pepper


def _make_device_token(rt: Runtime) -> str:
    token = mint_token()
    repo.api_device_create(rt.db, name="test", token_hash=hash_secret(_pepper(rt), token))
    return token


def _client(rt: Runtime) -> TestClient:
    return TestClient(build_app(rt))


class _RecordingSink:
    def __init__(self) -> None:
        self.tool_calls: list = []
        self.tool_results: list = []
        self.final: str | None = None
        self.error: Exception | None = None

    async def on_tool_call(self, name, args, call_id):
        self.tool_calls.append((name, args, call_id))

    async def on_tool_result(self, name, call_id, result):
        self.tool_results.append((name, call_id, result))

    async def on_final(self, text):
        self.final = text

    async def on_error(self, exc):
        self.error = exc


# --- owner.py refactor: parity + behaviour ----------------------------------

def test_run_owner_turn_final_and_context(tmp_path, monkeypatch):
    from archon.agent import owner

    rt = make_rt(tmp_path)

    async def fake_run_agent(router, registry, ctx, **kw):
        return "stub reply"

    monkeypatch.setattr(owner, "run_agent", fake_run_agent)
    sink = _RecordingSink()
    asyncio.run(owner.run_owner_turn(rt, "hello", sink))

    assert sink.final == "stub reply"
    assert sink.error is None
    chat_pk = owner._control_chat_pk(rt)
    contents = [(r["role"], r["content"]) for r in repo.context_get(rt.db, chat_pk, None)]
    assert ("user", "hello") in contents
    assert ("assistant", "stub reply") in contents


def test_handle_owner_text_still_answers(tmp_path, monkeypatch):
    from archon.agent import owner

    rt = make_rt(tmp_path)

    async def fake_run_agent(router, registry, ctx, **kw):
        return "stub reply"

    monkeypatch.setattr(owner, "run_agent", fake_run_agent)

    class FakeMessage:
        def __init__(self, text: str) -> None:
            self.text = text
            self.answers: list[str] = []

        async def answer(self, text: str) -> None:
            self.answers.append(text)

    msg = FakeMessage("hi there")
    asyncio.run(owner.handle_owner_text(rt, msg))
    # Same final text the stubbed agent returned reaches message.answer (Telegram
    # behaviour unchanged: html.escape is a no-op for this plain text).
    assert msg.answers == ["stub reply"]


def test_run_owner_turn_provider_error_does_not_persist(tmp_path, monkeypatch):
    from archon.agent import owner

    rt = make_rt(tmp_path)

    async def boom(router, registry, ctx, **kw):
        raise ProviderError("budget exceeded")

    monkeypatch.setattr(owner, "run_agent", boom)
    sink = _RecordingSink()
    asyncio.run(owner.run_owner_turn(rt, "hello", sink))

    assert sink.final is None
    assert isinstance(sink.error, ProviderError)
    chat_pk = owner._control_chat_pk(rt)
    assert repo.context_get(rt.db, chat_pk, None) == []  # not saved on error


# --- agent.py: on_event surfaces tool calls/results -------------------------

class _ToolProvider:
    name = "fake"
    supports_tools = True
    supports_vision = True

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, model, system, messages, tools=None,
                       max_tokens=4096, json_only=False, native_web_search=False):
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                text="", tool_calls=[ToolCall(id="c1", name="ping", args={})],
                usage=Usage(1, 1), model=model, provider="fake", stop_reason="tool_use",
            )
        return LLMResult(text="ok", tool_calls=[], usage=Usage(1, 1), model=model,
                         provider="fake", stop_reason="end")


def test_run_agent_emits_events(tmp_path):
    rt = make_rt(tmp_path)
    rt.router._providers["gemini"] = _ToolProvider()

    registry = Registry()

    async def ping(ctx) -> str:
        return json.dumps({"pong": True})

    registry.add(Tool(name="ping", description="", handler=ping,
                      input_schema={"type": "object", "properties": {}},
                      scopes=frozenset({"owner"})))

    events: list = []

    async def on_event(e):
        events.append(e)

    ctx = ToolContext(rt=rt, scope="owner")
    out = asyncio.run(run_agent(
        rt.router, registry, ctx, system="s",
        messages=[ChatMessage(role="user", text="go")], on_event=on_event,
    ))
    assert out == "ok"
    assert isinstance(events[0], ToolCallEvent) and events[0].name == "ping"
    assert isinstance(events[1], ToolResultEvent) and "pong" in events[1].result


# --- generic tool endpoints -------------------------------------------------

def test_tools_endpoint_lists_all_owner_tools(tmp_path):
    rt = make_rt(tmp_path)
    token = _make_device_token(rt)
    r = _client(rt).get("/tools", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    tools = r.json()["tools"]
    names = {t["name"] for t in tools}
    assert names == {s.name for s in rt.registry.specs_for("owner")}
    assert "system_status" in names
    by_name = {t["name"]: t for t in tools}
    assert by_name["db_backup_now"]["sensitive"] is True  # sensitive flag surfaced


def test_post_tool_dispatches(tmp_path):
    rt = make_rt(tmp_path)
    token = _make_device_token(rt)
    r = _client(rt).post("/tools/system_status", json={"args": {}},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    payload = json.loads(r.json()["result"])  # dispatch returns a JSON string
    assert "uptime_s" in payload and "subsystems" in payload


# --- device auth ------------------------------------------------------------

def test_require_device_rejects_missing_bad_and_revoked(tmp_path):
    rt = make_rt(tmp_path)
    client = _client(rt)
    assert client.get("/tools").status_code == 401  # missing
    assert client.get("/tools", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/tools", headers={"Authorization": "Basic x"}).status_code == 401

    token = mint_token()
    dev_id = repo.api_device_create(rt.db, name="d", token_hash=hash_secret(_pepper(rt), token))
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.get("/tools", headers=hdr).status_code == 200
    repo.api_device_revoke(rt.db, dev_id)
    assert client.get("/tools", headers=hdr).status_code == 401  # revoked


# --- pairing ----------------------------------------------------------------

def _future(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def test_pair_happy_path_mints_working_token_single_use(tmp_path):
    rt = make_rt(tmp_path)
    client = _client(rt)
    code = "123456"
    repo.api_pair_code_create(rt.db, code_hash=hash_secret(_pepper(rt), code),
                              expires_at=_future(10))
    r = client.post("/pair", json={"code": code, "device_name": "phone"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert r.json()["device_id"] > 0
    # minted token authenticates a real request
    assert client.get("/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # single-use: the same code cannot be redeemed again
    assert client.post("/pair", json={"code": code, "device_name": "phone2"}).status_code == 400


def test_pair_expired_code_rejected(tmp_path):
    rt = make_rt(tmp_path)
    code = "999999"
    repo.api_pair_code_create(rt.db, code_hash=hash_secret(_pepper(rt), code),
                              expires_at=_future(-1))  # already expired
    assert _client(rt).post("/pair", json={"code": code, "device_name": "x"}).status_code == 400


# --- streaming Mind endpoint ------------------------------------------------

def test_agent_chat_streams_events_and_final(tmp_path, monkeypatch):
    from archon.agent import owner

    rt = make_rt(tmp_path)

    async def fake_run_agent(router, registry, ctx, **kw):
        on_event = kw.get("on_event")
        if on_event is not None:
            await on_event(ToolCallEvent(name="ping", call_id="c1", args={}))
            await on_event(ToolResultEvent(name="ping", call_id="c1", result="{}"))
        return "streamed reply"

    monkeypatch.setattr(owner, "run_agent", fake_run_agent)
    token = _make_device_token(rt)
    r = _client(rt).post("/agent/chat", json={"text": "hi"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.text
    assert "event: tool_call" in body
    assert "event: tool_result" in body
    assert "event: final" in body
    assert "streamed reply" in body


def test_agent_chat_requires_auth(tmp_path):
    rt = make_rt(tmp_path)
    assert _client(rt).post("/agent/chat", json={"text": "hi"}).status_code == 401
