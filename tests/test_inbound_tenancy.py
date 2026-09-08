"""Per-tenant inbound: each linked user's Gmail is polled with their own
credentials and watermark, and every message is gated, cached, triaged and
agent-run under that user's tenant.

Nothing here touches the network: the Gmail client is a stub and the LLM router
is a fake, so the whole path from "Google returned a message" to "the agent ran
under tenant B" is exercised in-process.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope, owner_scope
from archon.integrations import google as gi
from archon.models import InboundMessage
from archon.pipeline import ingest
from archon.platforms.gmail import poller

from test_api import make_rt
from test_integrations_google import _fake_exchange, _rt
from test_tenancy import _new_tenant


# --- fakes -------------------------------------------------------------------

class FakeGmail:
    """Stands in for GmailClient; hands back one canned message per tenant."""

    def __init__(self, messages):
        self._messages = messages

    def list_messages(self, query, limit):
        return [{"id": m["id"]} for m in self._messages]

    def get_message(self, msg_id):
        return next(m for m in self._messages if m["id"] == msg_id)


def _gmail_message(msg_id: str, sender: str, subject: str, body: str,
                   when: datetime | None = None) -> dict:
    when = when or datetime.now(UTC)
    return {
        "id": msg_id,
        "threadId": f"t-{msg_id}",
        "internalDate": str(int(when.timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": f'"{sender}" <{sender}>'},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": f"<{msg_id}@mail>"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64(body)},
            "parts": [],
        },
    }


def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _link(rt, tenant_id, refresh):
    _, state = gi.authorize_url(rt, tenant_id)
    gi.complete_link(rt, state=state, code="c",
                     exchange=_fake_exchange(refresh=refresh))


# --- polling -----------------------------------------------------------------

def test_polling_tenants_lists_every_linked_account(tmp_path):
    rt = _rt(tmp_path)
    assert poller.polling_tenants(rt) == []          # owner has no token file yet

    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, b_id, "B-refresh")
    _link(rt, c_id, "C-refresh")
    assert poller.polling_tenants(rt) == [b_id, c_id]

    # the owner joins as soon as their file token exists, without linking
    rt.settings.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    rt.settings.google_token_path.write_text("{}", encoding="utf-8")
    assert poller.polling_tenants(rt) == [OWNER_TENANT_ID, b_id, c_id]


def test_a_revoked_link_stops_being_polled(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "B-refresh")
    assert poller.polling_tenants(rt) == [b_id]

    asyncio.run(gi.unlink(rt, b_id))
    assert poller.polling_tenants(rt) == []


def test_each_tenant_is_polled_with_their_own_creds_and_watermark(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, b_id, "B-refresh")
    _link(rt, c_id, "C-refresh")

    seen_refresh: list[str] = []

    def fake_client(auth, **_kw):
        seen_refresh.append(auth._store.load()["refresh_token"])
        tid = 2 if seen_refresh[-1] == "B-refresh" else 3
        return FakeGmail([_gmail_message(f"m-{tid}", f"s{tid}@x.com", "Hi", "body")])

    monkeypatch.setattr(poller, "GmailClient", fake_client)

    published: list[InboundMessage] = []
    monkeypatch.setattr(rt.bus, "publish",
                        lambda m: published.append(m) or asyncio.sleep(0))

    # Simulate accounts linked a while ago; on a tenant's FIRST poll the
    # watermark is set to "now" and everything already in the inbox is
    # deliberately skipped (see test_bootstrap_watermark_skips_older_mail).
    earlier = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    for tid in (b_id, c_id):
        repo.gmail_state_set(TenantScope(rt.db, tid), "bootstrapped", earlier)

    asyncio.run(poller._poll_tenant(rt, b_id))
    asyncio.run(poller._poll_tenant(rt, c_id))

    # each poll used that tenant's credential
    assert seen_refresh == ["B-refresh", "C-refresh"]
    # and each published message is stamped with its tenant
    assert [m.tenant_id for m in published] == [b_id, c_id]

    # watermarks are per-tenant and independent
    b_store, c_store = TenantScope(rt.db, b_id), TenantScope(rt.db, c_id)
    assert repo.gmail_state_get(b_store, "last_poll_at") is not None
    assert repo.gmail_state_get(c_store, "last_poll_at") is not None
    assert repo.gmail_state_get(owner_scope(rt.db), "bootstrapped") is None
    repo.gmail_state_set(b_store, "bootstrapped", "2099-01-01T00:00:00+00:00")
    assert repo.gmail_state_get(c_store, "bootstrapped") != "2099-01-01T00:00:00+00:00"


def test_bootstrap_watermark_skips_older_mail(tmp_path, monkeypatch):
    """Linking an account must not replay the user's back-catalogue."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "B-refresh")

    old = datetime.now(UTC) - timedelta(days=2)
    monkeypatch.setattr(poller, "GmailClient", lambda auth, **_kw: FakeGmail([
        _gmail_message("old", "s@x.com", "Old", "body", when=old),
        _gmail_message("new", "s@x.com", "New", "body"),
    ]))
    published: list[InboundMessage] = []
    monkeypatch.setattr(rt.bus, "publish",
                        lambda m: published.append(m) or asyncio.sleep(0))

    # Already-linked account: watermark set an hour ago.
    repo.gmail_state_set(TenantScope(rt.db, b_id), "bootstrapped",
                         (datetime.now(UTC) - timedelta(hours=1)).isoformat())

    assert asyncio.run(poller._poll_tenant(rt, b_id)) == 1
    assert [m.msg_id for m in published] == ["new"]


def test_first_poll_does_not_replay_an_existing_inbox(tmp_path, monkeypatch):
    """On a brand-new link the watermark is 'now', so nothing already sitting in
    the inbox is dragged through triage."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "B-refresh")

    monkeypatch.setattr(poller, "GmailClient", lambda auth, **_kw: FakeGmail([
        _gmail_message("already-there", "s@x.com", "Old", "body",
                       when=datetime.now(UTC) - timedelta(hours=2)),
    ]))
    published: list[InboundMessage] = []
    monkeypatch.setattr(rt.bus, "publish",
                        lambda m: published.append(m) or asyncio.sleep(0))

    assert asyncio.run(poller._poll_tenant(rt, b_id)) == 0
    assert published == []
    assert repo.gmail_state_get(TenantScope(rt.db, b_id), "bootstrapped") is not None


def test_one_tenants_failure_does_not_stop_the_others(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, b_id, "B-refresh")
    _link(rt, c_id, "C-refresh")

    polled: list[int] = []

    async def flaky(rt_, tenant_id):
        polled.append(tenant_id)
        if tenant_id == b_id:
            raise RuntimeError("token revoked upstream")
        return 0

    monkeypatch.setattr(poller, "_poll_tenant", flaky)

    async def one_round():
        # run() loops forever; drive a single round by making sleep bail out
        async def stop(_):
            raise asyncio.CancelledError

        monkeypatch.setattr(poller.asyncio, "sleep", stop)
        with __import__("pytest").raises(asyncio.CancelledError):
            await poller.run(rt)

    asyncio.run(one_round())
    assert polled == [b_id, c_id]                  # C still polled after B failed
    assert "failing" in rt.health["gmail"]
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "gmail_poll_error" in actions


# --- ingest ------------------------------------------------------------------

def _inbound(tenant_id, chat_id="s@x.com", msg_id="m1", text="hello"):
    return InboundMessage(
        platform="gmail", source="gmail", chat_id=chat_id, chat_kind="email",
        chat_name=chat_id, msg_id=msg_id, sender_id=chat_id, sender_name="S",
        ts=datetime.now(UTC), text=text, tenant_id=tenant_id,
    )


async def _consume_one(rt, msg):
    """Push one message through ingest.run's body without the infinite loop."""
    await rt.bus.publish(msg)
    task = asyncio.create_task(ingest.run(rt))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_inbound_is_cached_under_its_own_tenant(tmp_path):
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    asyncio.run(_consume_one(rt, _inbound(b_id, text="B's private mail")))

    b_store = TenantScope(rt.db, b_id)
    b_chat = repo.chat_get(b_store, "gmail", "s@x.com")
    assert b_chat is not None
    assert [m["text"] for m in repo.message_history(b_store, b_chat["id"])] \
        == ["B's private mail"]

    # the owner sees none of it — not the chat, not the message
    assert repo.chat_get(owner_scope(rt.db), "gmail", "s@x.com") is None
    assert repo.chat_list(owner_scope(rt.db)) == []


def test_two_tenants_same_correspondent_stay_separate(tmp_path):
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")

    async def scenario():
        await _consume_one(rt, _inbound(b_id, msg_id="shared-id", text="to B"))
        await _consume_one(rt, _inbound(c_id, msg_id="shared-id", text="to C"))

    asyncio.run(scenario())

    b_store, c_store = TenantScope(rt.db, b_id), TenantScope(rt.db, c_id)
    b_chat = repo.chat_get(b_store, "gmail", "s@x.com")
    c_chat = repo.chat_get(c_store, "gmail", "s@x.com")
    assert b_chat["id"] != c_chat["id"]           # same address, two chats
    assert [m["text"] for m in repo.message_history(b_store, b_chat["id"])] == ["to B"]
    assert [m["text"] for m in repo.message_history(c_store, c_chat["id"])] == ["to C"]


def test_the_gate_reads_the_messages_own_tenants_settings(tmp_path):
    """Tenant B turning Gmail triage off must not change the owner's gate."""
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    repo.setting_set(TenantScope(rt.db, b_id), "gmail.triage_enabled", False)

    allowed_b, reason_b = ingest.decide(rt, None, _inbound(b_id))
    allowed_a, reason_a = ingest.decide(rt, None, _inbound(OWNER_TENANT_ID))
    assert (allowed_b, reason_b) == (False, "gmail_triage_disabled")
    assert (allowed_a, reason_a) == (True, "gmail_triage_enabled")


def test_whitelist_is_evaluated_per_tenant(tmp_path):
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    b_store = TenantScope(rt.db, b_id)

    pk = repo.chat_upsert(b_store, "wa", "grp@g.us", "Grp", "group")
    repo.chat_set_field(b_store, pk, "is_whitelisted", 1)
    repo.chat_upsert(owner_scope(rt.db), "wa", "grp@g.us", "Grp", "group")

    wa_b = InboundMessage(platform="wa", source="wa", chat_id="grp@g.us",
                          chat_kind="group", msg_id="x", sender_id="s",
                          ts=datetime.now(UTC), text="hi", tenant_id=b_id)
    wa_a = InboundMessage(platform="wa", source="wa", chat_id="grp@g.us",
                          chat_kind="group", msg_id="x", sender_id="s",
                          ts=datetime.now(UTC), text="hi",
                          tenant_id=OWNER_TENANT_ID)
    assert ingest.decide(rt, repo.chat_get(b_store, "wa", "grp@g.us"), wa_b)[0] is True
    assert ingest.decide(rt, repo.chat_get(owner_scope(rt.db), "wa", "grp@g.us"),
                         wa_a) == (False, "not_whitelisted")


def test_debounce_never_batches_two_tenants_together(tmp_path):
    """chat_key carries the tenant, so a shared group cannot merge two tenants'
    messages into one triage batch."""
    b_msg = _inbound(2, chat_id="grp@g.us")
    c_msg = _inbound(3, chat_id="grp@g.us")
    assert b_msg.chat_key != c_msg.chat_key
    assert b_msg.chat_key == (2, "gmail", "grp@g.us")


def test_gate_audit_rows_belong_to_the_messages_tenant(tmp_path):
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    asyncio.run(_consume_one(rt, _inbound(b_id, text="B-only-subject")))

    b_actions = [r["action"] for r in repo.audit_query(TenantScope(rt.db, b_id),
                                                       limit=50)]
    a_blob = str([dict(r) for r in repo.audit_query(owner_scope(rt.db), limit=50)])
    assert b_actions                                   # B has gate rows
    assert "B-only-subject" not in a_blob              # the owner sees none of it
