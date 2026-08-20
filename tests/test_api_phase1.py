"""Phase 1 control-API tests: EventHub fan-out, the confirm-gate refactor
(single-use resolve_action + notifier registry), and the ergonomic REST
overlays. Every write still goes through Registry.dispatch. No network."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from archon.db import repo
from archon.events import EventHub
from archon.pipeline import confirm
from archon.tools import settings_ as settings_tools

from test_api import _client, _make_device_token, make_rt


def _auth(rt) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_device_token(rt)}"}


def _rt_with_gate_tools(tmp_path):
    """Runtime whose registry also carries the per-chat gate + settings tools
    the REST overlays dispatch to."""
    rt = make_rt(tmp_path)
    settings_tools.register(rt.registry)
    return rt


# --- EventHub ---------------------------------------------------------------

def test_eventhub_fans_out_and_unsubscribes():
    async def scenario():
        hub = EventHub()
        with hub.subscription() as q1, hub.subscription() as q2:
            hub.publish("message.new", chat_pk=1)
            assert q1.get_nowait()["kind"] == "message.new"
            assert q2.get_nowait()["data"] == {"chat_pk": 1}
        assert hub.subscriber_count == 0
        hub.publish("message.new")  # no subscribers → no error

    asyncio.run(scenario())


def test_eventhub_drops_oldest_and_never_blocks():
    async def scenario():
        hub = EventHub(maxsize=3)
        with hub.subscription() as q:
            for i in range(10):
                hub.publish("cost.update", n=i)  # would block a bounded Queue
            assert q.qsize() == 3
            # oldest dropped: the three most recent survive
            assert [q.get_nowait()["data"]["n"] for _ in range(3)] == [7, 8, 9]

    asyncio.run(scenario())


def test_eventhub_publish_allows_kind_payload_field():
    hub = EventHub()
    event = hub.publish("approval.pending", action_kind="wa.send", kind="not-the-event")
    assert event["kind"] == "approval.pending"
    assert event["data"]["kind"] == "not-the-event"


def test_eventhub_publish_survives_a_bad_subscriber():
    async def scenario():
        hub = EventHub()
        with hub.subscription():
            hub._subscribers.add("not a queue")  # type: ignore[arg-type]
            hub.publish("health.change", subsystem="api")  # must not raise

    asyncio.run(scenario())


# --- confirm gate refactor ---------------------------------------------------

def _pending(rt, kind="test.kind", minutes=60) -> int:
    return asyncio.run(confirm.request_confirmation(
        rt, kind=kind, payload={"a": 1}, description="d"))


def test_resolve_action_is_single_use(tmp_path):
    rt = make_rt(tmp_path)
    ran: list[dict] = []

    async def executor(rt_, payload):
        ran.append(payload)
        return "sent"

    confirm.register_executor("single.use", executor)
    action_id = _pending(rt, kind="single.use")

    first = asyncio.run(confirm.resolve_action(rt, action_id, "ok", actor="telegram"))
    second = asyncio.run(confirm.resolve_action(rt, action_id, "ok", actor="api"))
    assert first.status == "approved" and first.ok and first.detail == "sent"
    assert second.status == "already" and not second.ok
    assert ran == [{"a": 1}]  # executor ran exactly once
    assert repo.pending_action_get(rt.db, action_id)["status"] == "approved"


def test_resolve_action_reject_expired_and_unknown(tmp_path):
    rt = make_rt(tmp_path)

    action_id = _pending(rt)
    out = asyncio.run(confirm.resolve_action(rt, action_id, "no", actor="api"))
    assert out.status == "rejected"
    assert repo.pending_action_get(rt.db, action_id)["status"] == "rejected"

    assert asyncio.run(
        confirm.resolve_action(rt, 99999, "ok", actor="api")
    ).status == "unknown"

    past = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    stale = repo.pending_action_create(rt.db, kind="k", payload_json="{}",
                                       chat_pk=None, expires_at=past)
    assert asyncio.run(
        confirm.resolve_action(rt, stale, "ok", actor="api")
    ).status == "expired"
    assert repo.pending_action_get(rt.db, stale)["status"] == "expired"


def test_resolve_action_failed_executor_still_closes(tmp_path):
    rt = make_rt(tmp_path)

    async def boom(rt_, payload):
        raise RuntimeError("send failed")

    confirm.register_executor("boom.kind", boom)
    action_id = _pending(rt, kind="boom.kind")
    out = asyncio.run(confirm.resolve_action(rt, action_id, "ok", actor="api"))
    assert out.status == "approved" and out.ok is False
    assert "send failed" in out.detail
    assert repo.pending_action_get(rt.db, action_id)["status"] == "approved"


def test_request_confirmation_publishes_and_notifies(tmp_path):
    rt = make_rt(tmp_path)
    seen: list[tuple] = []

    async def notifier(rt_, action_id, kind, description, payload):
        seen.append((action_id, kind, description))

    confirm.register_notifier(notifier)
    try:
        async def scenario():
            with rt.events.subscription() as q:
                action_id = await confirm.request_confirmation(
                    rt, kind="wa.send", payload={"x": 1}, description="to Dana")
                event = q.get_nowait()
                return action_id, event

        action_id, event = asyncio.run(scenario())
    finally:
        confirm._NOTIFIERS.remove(notifier)

    assert seen == [(action_id, "wa.send", "to Dana")]
    assert event["kind"] == "approval.pending"
    assert event["data"]["action_kind"] == "wa.send"


def test_failing_notifier_does_not_lose_the_action(tmp_path):
    rt = make_rt(tmp_path)

    async def broken(rt_, action_id, kind, description, payload):
        raise RuntimeError("push down")

    confirm.register_notifier(broken)
    try:
        action_id = _pending(rt)
    finally:
        confirm._NOTIFIERS.remove(broken)
    assert repo.pending_action_get(rt.db, action_id)["status"] == "pending"


# --- approvals REST ----------------------------------------------------------

def test_approvals_list_and_decision(tmp_path):
    rt = make_rt(tmp_path)
    ran: list[dict] = []

    async def executor(rt_, payload):
        ran.append(payload)
        return "ok!"

    confirm.register_executor("api.approve", executor)
    action_id = _pending(rt, kind="api.approve")
    client, headers = _client(rt), _auth(rt)

    listed = client.get("/approvals", headers=headers).json()
    assert [a["id"] for a in listed] == [action_id]
    assert listed[0]["payload"] == {"a": 1}

    r = client.post(f"/approvals/{action_id}/decision", json={"ok": True}, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"status": "approved", "detail": "ok!", "ok": True}
    assert ran == [{"a": 1}]
    # second decision (e.g. the Telegram button) is refused
    again = client.post(f"/approvals/{action_id}/decision", json={"ok": True},
                        headers=headers)
    assert again.json()["status"] == "already"
    assert client.get("/approvals", headers=headers).json() == []


def test_approvals_requires_auth(tmp_path):
    rt = make_rt(tmp_path)
    assert _client(rt).get("/approvals").status_code == 401


# --- chats REST --------------------------------------------------------------

def test_chats_list_get_and_messages(tmp_path):
    rt = _rt_with_gate_tools(tmp_path)
    pk = repo.chat_upsert(rt.db, "wa", "c@g.us", "Family", "group")
    repo.chat_upsert(rt.db, "tg", "42", "Dana", "private")
    client, headers = _client(rt), _auth(rt)

    rows = client.get("/chats", headers=headers).json()
    assert {r["chat_id"] for r in rows} == {"c@g.us", "42"}
    assert {r["platform"] for r in client.get(
        "/chats?platform=wa", headers=headers).json()} == {"wa"}

    one = client.get(f"/chats/{pk}", headers=headers).json()
    assert one["name"] == "Family" and one["is_whitelisted"] is False
    assert client.get("/chats/9999", headers=headers).status_code == 404

    assert client.get(f"/chats/{pk}/messages", headers=headers).json() == []


def test_patch_chat_goes_through_tool_dispatch(tmp_path):
    rt = _rt_with_gate_tools(tmp_path)
    pk = repo.chat_upsert(rt.db, "wa", "c@g.us", "Family", "group")
    client, headers = _client(rt), _auth(rt)

    r = client.patch(f"/chats/{pk}",
                     json={"is_whitelisted": True, "send_policy": "free",
                           "auto_reply": True},
                     headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["chat"]["is_whitelisted"] is True
    assert body["chat"]["send_policy"] == "free"
    assert body["chat"]["auto_reply"] is True
    assert set(body["applied"]) == {"is_whitelisted", "send_policy", "auto_reply"}

    # the write is audited exactly like a Telegram-issued change: one 'tool'
    # audit row per dispatched gate tool, carrying the tool's own name
    tooled = {
        r_["action"]
        for r_ in rt.db.query("SELECT action FROM audit WHERE actor = 'tool'")
    }
    assert {"whitelist_add", "send_policy_set", "auto_reply_set"} <= tooled

    # turning the whitelist back off dispatches the remove tool
    off = client.patch(f"/chats/{pk}", json={"is_whitelisted": False}, headers=headers)
    assert off.json()["chat"]["is_whitelisted"] is False
    assert client.patch(f"/chats/{pk}", json={}, headers=headers).status_code == 400


# --- config / costs / schedules / contacts ----------------------------------

def test_config_get_redacts_and_put_dispatches(tmp_path):
    rt = _rt_with_gate_tools(tmp_path)
    repo.setting_set(rt.db, "llm.key.gemini", "super-secret")
    repo.setting_set(rt.db, "gmail.triage_enabled", True)
    client, headers = _client(rt), _auth(rt)

    settings = client.get("/config", headers=headers).json()["settings"]
    assert settings["llm.key.gemini"] == "•••"  # never hand back provider keys
    assert settings["gmail.triage_enabled"] is True

    r = client.put("/config/gmail.triage_enabled", json={"value": False}, headers=headers)
    assert r.status_code == 200 and json.loads(r.json()["result"])["ok"] is True
    assert repo.setting_get(rt.db, "gmail.triage_enabled") is False


def test_costs_windows_and_breakdown(tmp_path):
    rt = make_rt(tmp_path)
    repo.llm_call_record(rt.db, purpose="agent", provider="gemini", model="flash",
                         in_tokens=100, out_tokens=20, cost_usd=0.5)
    client, headers = _client(rt), _auth(rt)

    body = client.get("/costs?window=day", headers=headers).json()
    assert body["window"] == "day"
    assert body["total"]["calls"] == 1 and body["total"]["cost_usd"] == 0.5
    assert body["breakdown"][0]["provider"] == "gemini"
    assert client.get("/costs?window=nope", headers=headers).status_code == 422


def test_schedules_list_and_cancel(tmp_path):
    from archon.tools import scheduling as scheduling_tools

    rt = make_rt(tmp_path)
    scheduling_tools.register(rt.registry)
    pk = repo.chat_upsert(rt.db, "wa", "c@g.us", "Family", "group")
    cur = rt.db.execute(
        "INSERT INTO scheduled_messages (platform, chat_pk, text, due_at) "
        "VALUES ('wa', ?, 'hi', '2030-01-01 10:00:00')", (pk,))
    sched_id = int(cur.lastrowid)
    client, headers = _client(rt), _auth(rt)

    rows = client.get("/schedules", headers=headers).json()
    assert rows[0]["id"] == sched_id and rows[0]["chat_id"] == "c@g.us"

    r = client.delete(f"/schedules/{sched_id}", headers=headers)
    assert json.loads(r.json()["result"])["ok"] is True
    assert client.get("/schedules", headers=headers).json()[0]["status"] == "cancelled"


def test_contacts_list_and_sync(tmp_path):
    from archon.tools import contacts as contacts_tools

    rt = make_rt(tmp_path)
    contacts_tools.register(rt.registry)
    client, headers = _client(rt), _auth(rt)

    empty = client.get("/contacts", headers=headers).json()
    assert empty["contacts"] == [] and empty["entries"] == 0

    r = client.post("/contacts/sync", headers=headers, json={"contacts": [
        {"name": "Dana", "phone": "+972501234567"},
        {"name": "", "phone": "+972500000000"},  # skipped
    ]})
    assert r.status_code == 200
    assert r.json() == {"submitted": 2, "stored": 1, "skipped": 1}

    listed = client.get("/contacts", headers=headers).json()
    assert [c["name"] for c in listed["contacts"]] == ["Dana"]
    assert listed["entries"] == 1


# --- /stream WebSocket -------------------------------------------------------

def test_stream_requires_a_valid_token(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    rt = make_rt(tmp_path)
    client = _client(rt)
    for url in ("/stream", "/stream?token=bogus"):
        try:
            with client.websocket_connect(url):
                raise AssertionError(f"{url} should have been rejected")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_stream_delivers_events(tmp_path):
    rt = make_rt(tmp_path)
    token = _make_device_token(rt)
    with _client(rt).websocket_connect(f"/stream?token={token}") as ws:
        rt.events.publish("health.change", subsystem="api", state="running")
        event = ws.receive_json()
    assert event["kind"] == "health.change"
    assert event["data"] == {"subsystem": "api", "state": "running"}
    assert rt.events.subscriber_count == 0  # unsubscribed on disconnect


# --- concurrent approval race ------------------------------------------------

def test_two_concurrent_approvals_execute_exactly_once(tmp_path):
    """The plan's key guarantee: whichever channel approves first wins and the
    executor runs once, even when Telegram and the app decide simultaneously."""
    rt = make_rt(tmp_path)
    ran: list[str] = []

    async def slow_executor(rt_, payload):
        await asyncio.sleep(0.01)  # widen the window between claim and finish
        ran.append("executed")
        return "sent"

    confirm.register_executor("race.kind", slow_executor)
    action_id = _pending(rt, kind="race.kind")

    async def scenario():
        return await asyncio.gather(
            confirm.resolve_action(rt, action_id, "ok", actor="telegram"),
            confirm.resolve_action(rt, action_id, "ok", actor="api:device:1"),
        )

    first, second = asyncio.run(scenario())
    statuses = sorted([first.status, second.status])
    assert statuses == ["already", "approved"]
    assert ran == ["executed"]  # exactly one executor run


# --- ntfy push notifier (Part C) --------------------------------------------

class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that records posts instead of sending."""

    posts: list[dict] = []
    fail_with: Exception | None = None
    status_code: int = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json=None, headers=None):
        if type(self).fail_with is not None:
            raise type(self).fail_with
        type(self).posts.append({"url": url, "json": json, "headers": headers or {}})
        status = type(self).status_code

        class _Resp:
            status_code = status

        return _Resp()


def _patch_httpx(monkeypatch):
    from archon.api import push as push_mod

    _FakeAsyncClient.posts = []
    _FakeAsyncClient.fail_with = None
    _FakeAsyncClient.status_code = 200
    monkeypatch.setattr(push_mod.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def _rt_with_push(tmp_path, url: str, topic: str | None = "archon-abc", token: str = ""):
    rt = make_rt(tmp_path)
    rt.settings.ntfy_base_url = url
    rt.settings.ntfy_auth_token = token
    repo.api_device_create(rt.db, name="phone", token_hash="h1", push_endpoint=topic)
    return rt


def test_push_is_a_no_op_when_unconfigured(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="")  # ntfy_base_url empty -> disabled
    assert push.enabled(rt) is False
    assert asyncio.run(push.notify(rt, title="t", message="m")) == 0
    asyncio.run(push.ntfy_confirm_notifier(rt, 7, "wa.send", "to Dana", {"text": "hi"}))
    assert fake.posts == []  # nothing attempted at all


def test_push_is_a_no_op_when_device_has_no_topic(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="http://ntfy", topic=None)
    assert asyncio.run(push.notify(rt, title="t", message="m")) == 0
    assert fake.posts == []


def test_push_payload_shape_carries_id_but_no_content(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="http://10.8.0.1:8080/")
    asyncio.run(push.ntfy_confirm_notifier(
        rt, 42, "wa.send", "Send 'meet at 8' to Dana", {"text": "meet at 8"}))

    assert len(fake.posts) == 1
    post = fake.posts[0]
    # ntfy JSON publish: POST the BASE url with the topic inside the body.
    # Custom headers (X-Action-Id) are not forwarded by ntfy, so the action id
    # travels as a tag instead.
    assert post["url"] == "http://10.8.0.1:8080"  # trailing slash normalised
    body = post["json"]
    assert body["topic"] == "archon-abc"
    assert body["title"] == "Approval requested"
    assert body["message"] == "wa.send"  # action TYPE only
    assert push.action_id_from_tags(body["tags"]) == 42
    # The description and the message content never leave the VM this way.
    blob = str(post)
    for secret in ("Dana", "meet at 8"):
        assert secret not in blob


def test_push_sends_auth_token_when_configured(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="http://ntfy", token="tk_secret")
    asyncio.run(push.notify(rt, title="t", message="m"))
    assert fake.posts[0]["headers"]["Authorization"] == "Bearer tk_secret"

    fake2 = _patch_httpx(monkeypatch)
    rt2 = _rt_with_push(tmp_path / "b", url="http://ntfy")
    asyncio.run(push.notify(rt2, title="t", message="m"))
    assert "Authorization" not in fake2.posts[0]["headers"]


def test_push_one_request_per_device_with_topic(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="http://ntfy")
    repo.api_device_create(rt.db, name="tablet", token_hash="h2",
                           push_endpoint="archon-tablet")
    repo.api_device_create(rt.db, name="no-topic", token_hash="h3")
    revoked = repo.api_device_create(rt.db, name="old", token_hash="h4",
                                     push_endpoint="archon-dead")
    repo.api_device_revoke(rt.db, revoked)

    assert asyncio.run(push.notify(rt, title="t", message="m")) == 2
    assert sorted(p["json"]["topic"] for p in fake.posts) == [
        "archon-abc", "archon-tablet"]


def test_push_swallows_network_error_into_an_audit_note(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    fake.fail_with = RuntimeError("broker down")
    rt = _rt_with_push(tmp_path, url="http://ntfy")
    assert asyncio.run(push.notify(rt, title="t", message="m")) == 0  # never raises
    notes = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "push_failed" in notes


def test_push_treats_http_error_status_as_not_delivered(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    fake.status_code = 503
    rt = _rt_with_push(tmp_path, url="http://ntfy")
    assert asyncio.run(push.notify(rt, title="t", message="m")) == 0
    notes = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "push_rejected" in notes


def test_push_failure_does_not_propagate_out_of_request_confirmation(tmp_path, monkeypatch):
    """The gate must survive a dead broker: the pending row is still written."""
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    fake.fail_with = RuntimeError("broker down")
    rt = _rt_with_push(tmp_path, url="http://ntfy")
    push.register(rt)
    try:
        action_id = asyncio.run(confirm.request_confirmation(
            rt, kind="wa.send", payload={"text": "hi"}, description="to Dana"))
    finally:
        confirm._NOTIFIERS.remove(push.ntfy_confirm_notifier)
    assert repo.pending_action_get(rt.db, action_id)["status"] == "pending"


def test_confirm_gate_pushes_when_configured(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="http://ntfy")
    push.register(rt)
    try:
        action_id = asyncio.run(confirm.request_confirmation(
            rt, kind="event.create", payload={"title": "Dentist"},
            description="Dentist at 15:00"))
    finally:
        confirm._NOTIFIERS.remove(push.ntfy_confirm_notifier)

    assert len(fake.posts) == 1
    body = fake.posts[0]["json"]
    assert body["message"] == "event.create"
    assert push.action_id_from_tags(body["tags"]) == action_id
    assert "Dentist" not in str(fake.posts[0])


def test_owner_alert_publishes_event_and_pushes(tmp_path, monkeypatch):
    from archon.api import push

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="http://ntfy")

    async def scenario():
        with rt.events.subscription() as q:
            await push.owner_alert(rt, source="tg_notify_owner")
            return q.get_nowait()

    event = asyncio.run(scenario())
    assert event["kind"] == "owner.alert"
    assert event["data"] == {"source": "tg_notify_owner"}
    assert fake.posts[0]["json"]["title"] == "Archon alert"


def test_push_register_is_idempotent(tmp_path):
    from archon.api import push

    rt = _rt_with_push(tmp_path, url="http://ntfy")
    before = len(confirm._NOTIFIERS)
    push.register(rt)
    push.register(rt)
    try:
        assert len(confirm._NOTIFIERS) == before + 1
        assert push.ntfy_confirm_notifier in confirm._NOTIFIERS
    finally:
        confirm._NOTIFIERS.remove(push.ntfy_confirm_notifier)


def test_tg_notify_owner_also_pushes(tmp_path, monkeypatch):
    """The owner-alert path wakes the phone as well as sending to Telegram,
    without leaking the alert text into the push."""
    from archon.tools import telegram as telegram_tools
    from archon.tools.registry import Registry, ToolContext

    fake = _patch_httpx(monkeypatch)
    rt = _rt_with_push(tmp_path, url="http://ntfy")

    sent: list[tuple] = []

    class FakeBot:
        async def send_message(self, chat_id, text, parse_mode=None):
            sent.append((chat_id, text))

    rt.clients["notifier"] = FakeBot()
    registry = Registry()
    telegram_tools.register(registry)

    out = asyncio.run(registry.dispatch(
        ToolContext(rt=rt, scope="owner"), "tg_notify_owner",
        {"text": "disk almost full on the VM"}))

    assert json.loads(out) == {"ok": True}
    assert sent == [(rt.settings.telegram_owner_id, "disk almost full on the VM")]
    # Telegram carries the text; the push carries only "there is an alert".
    assert len(fake.posts) == 1
    assert "disk almost full" not in str(fake.posts[0])
