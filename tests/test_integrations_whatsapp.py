"""Per-tenant WhatsApp: the consent gate, session isolation and encryption at
rest, the deliberate absence of the fingerprint spoof, and lifecycle.

No neonize and no WhatsApp: the client factory is stubbed, so nothing here
attempts to link a real account.

The load-bearing tests are the consent ones. This integration knowingly breaks
WhatsApp's ToS and can get a user's number permanently banned; the product
owner accepted that, and the deal is that the risk is *consented*. A regression
that let linking start without acknowledgement would silently break that deal,
so it is asserted from several directions.
"""

from __future__ import annotations

import asyncio

import pytest

from archon.crypto import CredentialCryptoError, decrypt
from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope, owner_scope, tenant_purge
from archon.integrations import whatsapp as wa

from test_api import _client, _make_device_token, make_rt
from test_tenancy import _new_tenant

_KEY = "a" * 64


def _rt(tmp_path):
    rt = make_rt(tmp_path)
    rt.settings.multitenant_enabled = True
    rt.settings.credential_encryption_key = _KEY
    return rt


#: What the stubbed WhatsApp hands back. Real codes are 8 chars from a
#: confusable-free alphabet; the exact value is irrelevant here because it is
#: passed through verbatim and never compared.
_FAKE_PAIR_CODE = "ACDE1234"


@pytest.fixture(autouse=True)
def _no_live_whatsapp(monkeypatch):
    """Nothing in this file may talk to WhatsApp.

    ``start_link`` now asks a live client for a pairing code, so without this
    every consent test would try to stand up neonize. Stubbing at
    ``request_pair_code`` keeps the consent gate, phone validation and
    persistence under test while the one genuinely un-testable step — the
    conversation with WhatsApp's servers — is replaced.
    """
    async def _fake(_rt, _tenant_id, _digits):
        return _FAKE_PAIR_CODE

    monkeypatch.setattr(wa, "request_pair_code", _fake)


def _start(rt, tenant_id, *, phone="+972500000001", **kw):
    """Await ``start_link`` from a sync test.

    Worth knowing why this exists: ``start_link`` became a coroutine, and a
    coroutine that is called but never awaited raises NOTHING — so a test that
    forgot to await would report the consent gate as passing while the gate had
    not run at all. Routing every call through one helper removes the chance.
    """
    return asyncio.run(wa.start_link(rt, tenant_id, phone=phone, **kw))


def _linked(rt, tenant_id, session_bytes=b"session-blob"):
    """A tenant who consented, paired, and has a session on disk."""
    _start(rt, tenant_id, consent_acknowledged=True)
    path = wa.session_path(rt, tenant_id)
    path.write_bytes(session_bytes)
    wa.record_pair_status(rt, tenant_id, ok=True, phone_jid=f"{tenant_id}@s.whatsapp.net")
    return path


# --- the consent gate --------------------------------------------------------

def test_linking_is_refused_without_consent(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    for bad in (False, None, "yes", 1):
        with pytest.raises(wa.ConsentRequired):
            _start(rt, b_id, consent_acknowledged=bad)

    # nothing was created: no link row, no session, no directory
    assert repo.whatsapp_link_get(TenantScope(rt.db, b_id)) is None
    assert not wa.session_path(rt, b_id).exists()


def test_refusal_is_audited(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    with pytest.raises(wa.ConsentRequired):
        _start(rt, b_id, consent_acknowledged=False)
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_link_refused_no_consent" in actions


def test_consent_is_recorded_with_the_version_that_was_shown(tmp_path):
    """A consent record that does not say WHAT was agreed to is worthless once
    the wording changes."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id, consent_acknowledged=True)

    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["consent_version"] == wa.CONSENT_VERSION
    assert row["consent_acknowledged_at"] is not None

    # and independently in the audit log, so it survives the row being purged
    consent = [dict(r) for r in rt.db.query(
        "SELECT * FROM audit WHERE action = 'wa_consent_acknowledged'")]
    assert len(consent) == 1
    assert wa.CONSENT_VERSION in consent[0]["detail_json"]
    assert "permanent_account_ban" in consent[0]["detail_json"]


def test_a_stale_consent_version_is_refused(tmp_path):
    """If the warning has been reworded, the old acknowledgement no longer
    covers it and the user has to read the new one."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    with pytest.raises(wa.ConsentRequired, match="updated"):
        _start(rt, b_id, consent_acknowledged=True,
                      consent_version="1999-01-01.v0")


def test_the_warning_states_the_actual_risk(tmp_path):
    """The text is the product's honesty commitment; keep it explicit."""
    notice = wa.consent_notice()
    warning = notice["warning"].lower()
    assert "permanently ban" in warning
    assert "terms of service" in warning
    assert "at your own risk" in warning
    assert notice["reversible"] is False
    # it should also point at the safe alternatives
    assert "google" in warning and "telegram" in warning


def test_api_refuses_to_link_without_acknowledgement(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    headers = {"Authorization": f"Bearer {_make_device_token(rt)}"}

    # the default is refusal even if the field is omitted entirely
    assert client.post("/integrations/whatsapp/link", json={},
                       headers=headers).status_code == 400
    assert client.post("/integrations/whatsapp/link",
                       json={"consent_acknowledged": False},
                       headers=headers).status_code == 400
    assert repo.whatsapp_link_get(owner_scope(rt.db)) is None

    # Consent alone is no longer enough: pairing by code needs the number.
    # 400 (not 422) because the consent gate must be reached first — see
    # WhatsAppLinkRequest.phone for why the field is schema-optional.
    no_phone = client.post("/integrations/whatsapp/link",
                           json={"consent_acknowledged": True}, headers=headers)
    assert no_phone.status_code == 400
    assert "international format" in no_phone.json()["detail"]

    ok = client.post("/integrations/whatsapp/link",
                     json={"consent_acknowledged": True,
                           "phone": "+972500000001"}, headers=headers)
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "awaiting_code"
    assert body["pair_code"] == _FAKE_PAIR_CODE
    assert body["pair_code_expires_at"] is not None


def test_consent_endpoint_serves_the_verbatim_warning(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    headers = {"Authorization": f"Bearer {_make_device_token(rt)}"}
    body = client.get("/integrations/whatsapp/consent", headers=headers).json()
    assert body["version"] == wa.CONSENT_VERSION
    assert body["warning"] == wa.CONSENT_WARNING
    assert body["reversible"] is False


# --- the spoof is owner-only -------------------------------------------------

def test_product_tenants_do_not_get_the_android_fingerprint_spoof(tmp_path):
    """Many accounts sharing one forged device fingerprint is the correlated
    signal that turns a single ban into a sweep. Product tenants run plain."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    assert wa.client_props_for(rt, b_id) is None
    # the owner's own client keeps it (their view-once capture depends on it)
    assert wa.client_props_for(rt, OWNER_TENANT_ID) is not None


# --- session isolation and encryption at rest --------------------------------

def test_sessions_live_in_separate_per_tenant_directories(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    assert wa.session_path(rt, b_id) != wa.session_path(rt, c_id)
    assert str(b_id) in str(wa.session_path(rt, b_id))


def test_session_is_encrypted_at_rest_and_the_plaintext_removed(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    path = _linked(rt, b_id, session_bytes=b"SECRET-SESSION")

    assert wa.persist_session(rt, b_id, wipe=True) is True
    assert not path.exists()                       # plaintext gone

    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert "SECRET-SESSION" not in row["session_envelope"]
    # ...and it round-trips for its own tenant
    restored = wa.materialise_session(rt, b_id)
    assert restored.read_bytes() == b"SECRET-SESSION"


def test_another_tenant_cannot_decrypt_a_session(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _linked(rt, b_id, session_bytes=b"B-SESSION")
    wa.persist_session(rt, b_id, wipe=True)

    envelope = repo.whatsapp_link_get(TenantScope(rt.db, b_id))["session_envelope"]
    with pytest.raises(CredentialCryptoError):
        decrypt(_KEY, envelope, tenant_id=c_id, purpose="whatsapp")


def test_an_undecryptable_session_demands_a_relink_rather_than_failing_open(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id)
    wa.persist_session(rt, b_id, wipe=True)
    rt.db.execute(
        "UPDATE whatsapp_links SET session_envelope = ? WHERE tenant_id = ?",
        ('{"v":1,"alg":"AESGCM-256","dek":"AA","dn":"AA","ct":"AA","n":"AA"}', b_id))

    with pytest.raises(wa.WhatsAppLinkError, match="re-link"):
        wa.materialise_session(rt, b_id)
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_session_undecryptable" in actions


def test_two_tenants_sessions_never_share_a_client(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _linked(rt, b_id, b"B")
    _linked(rt, c_id, b"C")

    built: list[tuple[int, str]] = []

    def fake_factory(rt_, tenant_id):
        built.append((tenant_id, str(wa.session_path(rt_, tenant_id))))
        return {"tenant": tenant_id}

    rt.sessions.register_factory("whatsapp", fake_factory)

    async def scenario():
        return (await rt.sessions.get(rt, b_id, "whatsapp"),
                await rt.sessions.get(rt, c_id, "whatsapp"))

    b_client, c_client = asyncio.run(scenario())
    assert b_client is not c_client
    assert built[0][1] != built[1][1]              # different session files
    assert rt.sessions.live_count == 2


# --- pairing lifecycle -------------------------------------------------------

def test_pair_status_records_success_and_failure(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id, consent_acknowledged=True)
    assert wa.status(rt, TenantScope(rt.db, b_id))["status"] == "awaiting_code"

    wa.session_path(rt, b_id).write_bytes(b"s")
    wa.record_pair_status(rt, b_id, ok=True, phone_jid="4477@s.whatsapp.net")
    st = wa.status(rt, TenantScope(rt.db, b_id))
    assert st["linked"] is True and st["phone"] == "4477@s.whatsapp.net"

    c_id = _new_tenant(rt, "c@example.com")
    _start(rt, c_id, consent_acknowledged=True)
    wa.record_pair_status(rt, c_id, ok=False, error="QR expired")
    st_c = wa.status(rt, TenantScope(rt.db, c_id))
    assert st_c["linked"] is False and st_c["status"] == "failed"
    assert "QR expired" in st_c["last_error"]


def test_a_ban_is_recorded_distinctly_from_a_logout(tmp_path):
    """The user was warned about exactly this outcome; the app must be able to
    say 'banned', not 'disconnected'."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id)

    asyncio.run(wa.record_logged_out(rt, b_id, banned=True))
    assert wa.status(rt, TenantScope(rt.db, b_id))["status"] == "banned"
    assert not wa.session_path(rt, b_id).exists()
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_banned" in actions and "wa_logged_out" not in actions


def test_relinking_replaces_the_previous_session(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id, b"OLD")
    wa.persist_session(rt, b_id, wipe=True)

    _start(rt, b_id, consent_acknowledged=True)
    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["status"] == "awaiting_code"
    assert row["session_envelope"] is None        # the old session is gone
    assert not wa.session_path(rt, b_id).exists()
    # the previous consent row is retained as history
    assert len(rt.db.query("SELECT 1 FROM whatsapp_links WHERE tenant_id = ?",
                           (b_id,))) == 2


# --- unlink and purge --------------------------------------------------------

def test_unlink_evicts_the_session_and_wipes_it(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id)
    rt.sessions.register_factory("whatsapp", lambda rt_, tid: {"t": tid})

    async def scenario():
        await rt.sessions.get(rt, b_id, "whatsapp")
        assert rt.sessions.live_count == 1
        return await wa.unlink(rt, b_id)

    assert asyncio.run(scenario()) is True
    assert rt.sessions.live_count == 0
    assert not wa.session_path(rt, b_id).exists()
    assert repo.whatsapp_link_get(TenantScope(rt.db, b_id)) is None


def test_eviction_persists_the_session_before_dropping_it(tmp_path):
    """Idle eviction must not lose a paired session — the user would have to
    re-scan a QR, and each re-link is another ban-risk event."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id, b"KEEP-ME")
    wa.register(rt)
    rt.sessions.register_factory("whatsapp", lambda rt_, tid: {"t": tid},
                                 on_evict=wa._on_evict)

    async def scenario():
        await rt.sessions.get(rt, b_id, "whatsapp")
        await rt.sessions.evict(b_id, "whatsapp")

    asyncio.run(scenario())
    assert not wa.session_path(rt, b_id).exists()          # plaintext removed
    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["session_envelope"] is not None             # but not lost
    assert wa.materialise_session(rt, b_id).read_bytes() == b"KEEP-ME"


def test_purging_a_tenant_removes_their_whatsapp_link(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id)
    tenant_purge(rt.db, b_id)
    assert rt.db.query("SELECT 1 FROM whatsapp_links WHERE tenant_id = ?",
                       (b_id,)) == []


def test_linked_tenants_lists_only_paired_ones(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _linked(rt, b_id)
    _start(rt, c_id, consent_acknowledged=True)     # pending, not paired

    assert [r["tenant_id"] for r in repo.whatsapp_linked_tenants(rt.db)] == [b_id]


# --- API ---------------------------------------------------------------------

def test_status_and_unlink_endpoints(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    headers = {"Authorization": f"Bearer {_make_device_token(rt)}"}

    assert client.get("/integrations/whatsapp",
                      headers=headers).json()["status"] == "not_linked"

    _linked(rt, OWNER_TENANT_ID)
    st = client.get("/integrations/whatsapp", headers=headers).json()
    assert st["linked"] is True and st["consent_version"] == wa.CONSENT_VERSION

    assert client.delete("/integrations/whatsapp", headers=headers).status_code == 200
    assert client.get("/integrations/whatsapp",
                      headers=headers).json()["status"] == "not_linked"


def test_whatsapp_endpoints_require_auth(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    assert client.get("/integrations/whatsapp/consent").status_code == 401
    assert client.post("/integrations/whatsapp/link",
                       json={"consent_acknowledged": True}).status_code == 401
    assert client.get("/integrations/whatsapp").status_code == 401


# --- pairing by phone number (code, not QR) ----------------------------------

def test_phone_is_normalised_to_the_digits_whatsmeow_wants():
    """The API speaks E.164; whatsmeow wants bare digits. We absorb the gap."""
    assert wa.normalise_phone("+972501234567") == "972501234567"
    assert wa.normalise_phone("+972 50 123 4567") == "972501234567"
    assert wa.normalise_phone("+972-50-123-4567") == "972501234567"
    assert wa.normalise_phone("972501234567") == "972501234567"


@pytest.mark.parametrize("bad", [None, "", "   ", "+", "abc", "12345", "9" * 16])
def test_an_unusable_phone_is_refused_with_a_specific_type(bad):
    """``PhoneRequired``, not a generic link error.

    The router maps this type to 400 so the app re-prompts for the number
    instead of offering a blanket retry; matching on message text instead would
    break the first time someone rewords an error.
    """
    with pytest.raises(wa.PhoneRequired):
        wa.normalise_phone(bad)


def test_consent_is_checked_before_the_phone_is_even_looked_at(tmp_path):
    """Ordering the consent gate first is deliberate, not incidental.

    A request with neither consent nor phone must fail as a CONSENT refusal and
    write that audit row. If the phone were validated first, an attempt to link
    without consenting would be recorded as a formatting mistake — losing the
    one record that proves the gate held.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    with pytest.raises(wa.ConsentRequired):
        asyncio.run(wa.start_link(rt, b_id, consent_acknowledged=False, phone=None))

    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_link_refused_no_consent" in actions


def test_a_pair_code_is_issued_and_recorded(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    out = _start(rt, b_id, consent_acknowledged=True, phone="+972501234567")

    assert out["status"] == "awaiting_code"
    assert out["pair_code"] == _FAKE_PAIR_CODE
    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["pair_code"] == _FAKE_PAIR_CODE
    assert row["pair_code_expires_at"] is not None
    # The number tried is recorded up front, so a failure still says which one.
    assert row["phone_e164"] == "+972501234567"


def test_relinking_issues_a_fresh_code(tmp_path, monkeypatch):
    """The retry path when a code expires or the user fumbles it."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    first = _start(rt, b_id, consent_acknowledged=True)
    assert first["pair_code"] == _FAKE_PAIR_CODE

    issued: list[str] = []

    async def _counter(_rt, _tid, _digits):
        issued.append(f"CODE{len(issued)}")
        return issued[-1]

    monkeypatch.setattr(wa, "request_pair_code", _counter)
    second = _start(rt, b_id, consent_acknowledged=True)

    assert second["pair_code"] == "CODE0" != first["pair_code"]
    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["pair_code"] == "CODE0"
    # Re-linking starts a NEW row; the old one is revoked, not reused.
    assert row["status"] == "awaiting_code"


def test_the_code_is_cleared_once_it_can_no_longer_be_used(tmp_path):
    """A stale code on a resolved link keeps the app prompting for entry."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    _start(rt, b_id, consent_acknowledged=True)
    wa.record_pair_status(rt, b_id, ok=True, phone_jid="b@s.whatsapp.net")
    st = wa.status(rt, TenantScope(rt.db, b_id))
    assert st["pair_code"] is None and st["pair_code_expires_at"] is None
    assert st["linked"] is True and st["status"] == "paired"

    c_id = _new_tenant(rt, "c@example.com")
    _start(rt, c_id, consent_acknowledged=True)
    wa.record_pair_status(rt, c_id, ok=False, error="wrong code")
    st_c = wa.status(rt, TenantScope(rt.db, c_id))
    assert st_c["pair_code"] is None and st_c["status"] == "failed"


def test_status_survives_a_row_from_before_the_migration(tmp_path):
    """sqlite3.Row raises on an unknown column rather than returning None.

    A partially-migrated database would otherwise break ``status()`` outright
    — the endpoint the app polls — turning a schema lag into a dead screen.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id, consent_acknowledged=True)

    class _OldRow(dict):
        def __getitem__(self, k):
            if k in ("pair_code", "pair_code_expires_at", "phone_e164"):
                raise IndexError(k)
            return super().__getitem__(k)

    old = _OldRow(status="pending", phone_jid=None, consent_version="v",
                  consent_acknowledged_at="t", paired_at=None, last_error=None)
    real = repo.whatsapp_link_get
    repo.whatsapp_link_get = lambda _store: old
    try:
        st = wa.status(rt, TenantScope(rt.db, b_id))
    finally:
        repo.whatsapp_link_get = real
    assert st["pair_code"] is None and st["status"] == "pending"


def test_the_wire_contract_the_app_builds_against_is_frozen():
    """android-app codes to these exact names (task #17).

    Renaming a field here is not a refactor, it is a break in another agent's
    build — so the names are pinned rather than left to be discovered at
    integration time.
    """
    from archon.api.schemas import WhatsAppLinkRequest, WhatsAppStatus

    assert "phone" in WhatsAppLinkRequest.model_fields
    assert WhatsAppLinkRequest.model_fields["phone"].default is None

    for field in ("linked", "status", "phone", "consent_version",
                  "consent_acknowledged_at", "paired_at", "last_error",
                  "pair_code", "pair_code_expires_at"):
        assert field in WhatsAppStatus.model_fields, field


# --- per-tenant event wiring (the inbound/send flow) -------------------------

from neonize.events import (  # noqa: E402 — after the shared helpers above
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    TemporaryBanEv,
)


class _FakeClient:
    """Captures the handlers ``wire_events`` registers.

    Keyed by the event TYPE OBJECT, not its name: the ``…Ev`` names are aliases
    exported by ``neonize.events``, while the classes themselves are protobuf
    types called ``Message``, ``PairStatus`` and so on. Keying by name meant
    guessing, and guessing wrong produced a KeyError that looked like the wiring
    had failed.
    """

    def __init__(self) -> None:
        self.handlers: dict[object, object] = {}
        self.sent: list[tuple] = []

    def event(self, ev_type):
        def deco(fn):
            self.handlers[ev_type] = fn
            return fn
        return deco

    async def send_message(self, to, text):
        self.sent.append((to, text))
        return type("Sent", (), {"ID": "wamid.1"})()


def _wired(rt, tenant_id):
    client = _FakeClient()
    wa.wire_events(rt, tenant_id, client)
    return client


def _fire(client, ev_type, ev=None):
    """Invoke the handler registered for ``ev_type`` (a neonize.events alias)."""
    return asyncio.run(client.handlers[ev_type](None, ev))


def test_inbound_messages_are_stamped_with_the_owning_tenant(tmp_path, monkeypatch):
    """THE leak guard for per-tenant inbound.

    ``InboundMessage.tenant_id`` defaults to the OWNER, so a missing stamp does
    not fail loudly — it files a stranger's WhatsApp messages into the owner's
    chats, memory and agent context. This asserts the stamp rather than trusting
    the default.
    """
    from archon.models import InboundMessage

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    published: list[InboundMessage] = []

    class _Bus:
        async def publish(self, msg):
            published.append(msg)

    rt.bus = _Bus()

    from archon.platforms.whatsapp import events as wa_events
    from datetime import UTC, datetime

    def _fake_parse(_event):
        return InboundMessage(
            platform="wa", source="wa", chat_id="x@g.us", chat_kind="group",
            msg_id="m1", sender_id="s1", ts=datetime.now(UTC), text="hello",
        )

    monkeypatch.setattr(wa_events, "from_message_event", _fake_parse)

    client = _wired(rt, b_id)
    _fire(client, MessageEv, object())

    assert len(published) == 1
    assert published[0].tenant_id == b_id
    assert published[0].tenant_id != OWNER_TENANT_ID
    assert published[0].text == "hello"


def test_pair_success_and_failure_are_recorded_from_the_event(tmp_path):
    """PStatus SUCCESS=2 / ERROR=1 (neonize Neonize_pb2.PairStatus)."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id, consent_acknowledged=True)

    client = _wired(rt, b_id)
    _fire(client, PairStatusEv,
          type("Ev", (), {"Status": 2, "ID": None, "Error": ""})())
    st = wa.status(rt, TenantScope(rt.db, b_id))
    assert st["status"] == "paired" and st["linked"] is True
    assert st["pair_code"] is None            # spent, and cleared

    c_id = _new_tenant(rt, "c@example.com")
    _start(rt, c_id, consent_acknowledged=True)
    client_c = _wired(rt, c_id)
    _fire(client_c, PairStatusEv,
          type("Ev", (), {"Status": 1, "ID": None, "Error": "bad code"})())
    assert wa.status(rt, TenantScope(rt.db, c_id))["status"] == "failed"


def test_a_ban_event_is_recorded_as_a_ban_not_a_logout(tmp_path):
    """The outcome the consent warning names — the app must be able to say so."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id)

    _fire(_wired(rt, b_id), TemporaryBanEv, type("Ev", (), {})())
    assert wa.status(rt, TenantScope(rt.db, b_id))["status"] == "banned"

    c_id = _new_tenant(rt, "c@example.com")
    _linked(rt, c_id)
    _fire(_wired(rt, c_id), LoggedOutEv)
    assert wa.status(rt, TenantScope(rt.db, c_id))["status"] == "logged_out"


def test_a_handler_exception_never_reaches_neonize(tmp_path, monkeypatch):
    """An exception crossing back into the Go callback kills the session.

    A malformed message must cost one message, not the tenant's whole WhatsApp
    connection — so every handler swallows and audits instead of raising.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    from archon.platforms.whatsapp import events as wa_events

    def _boom(_event):
        raise ValueError("unparseable")

    monkeypatch.setattr(wa_events, "from_message_event", _boom)

    _fire(_wired(rt, b_id), MessageEv, object())      # must not raise
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_tenant_inbound_failed" in actions


def test_tenant_sends_are_paced(tmp_path):
    """Same budgets as the userbot; a refused send never reaches the transport."""
    from archon.pacing import PaceRefused

    rt = _rt(tmp_path)
    rt.settings.userbot_sends_per_min_per_peer = 2
    rt.settings.userbot_send_gap_s_min = 0.0
    rt.settings.userbot_send_gap_s_max = 0.0
    b_id = _new_tenant(rt, "b@example.com")

    client = _FakeClient()

    async def _get(_rt, _tid, _kind):
        return client

    rt.sessions.get = _get

    async def _run():
        for _ in range(2):
            await wa.send_message(rt, b_id, "x@s.whatsapp.net", "hi")
        with pytest.raises(PaceRefused):
            await wa.send_message(rt, b_id, "x@s.whatsapp.net", "hi")

    asyncio.run(_run())
    assert len(client.sent) == 2


# --- the live pairing failure: PairPhone raced connect() ---------------------

class _SlowClient:
    """A client whose Go side becomes ready only after N readiness checks.

    Models what neonize actually does: connect() returns a Task immediately
    while the Go client is still being constructed, so `is_connected` is False
    (or raises) for a while afterwards.
    """

    def __init__(self, ready_after: int = 3, raise_until_ready: bool = False):
        self.checks = 0
        self.ready_after = ready_after
        self.raise_until_ready = raise_until_ready
        self.paired_with: list[str] = []

    @property
    def is_connected(self) -> bool:
        self.checks += 1
        if self.checks < self.ready_after:
            if self.raise_until_ready:
                # The bridge call itself fails while the Go client is nil.
                raise RuntimeError("client is nil")
            return False
        return True

    def PairPhone(self, digits, show_push):        # noqa: N802 — neonize name
        if self.checks < self.ready_after:
            raise RuntimeError("client is nil")
        self.paired_with.append(digits)
        return "ACDE1234"


def test_pairing_waits_for_the_socket_instead_of_racing_it(tmp_path):
    """The live failure: PairPhoneError('client is nil').

    neonize's connect() ends with `create_task(...)` and returns the Task, so
    awaiting it yields a task that only finishes when the connection DIES. The
    Go client is still being built when it returns — and PairPhone fired into
    that gap. Readiness must be observed, not assumed.
    """
    rt = _rt(tmp_path)
    client = _SlowClient(ready_after=3)

    asyncio.run(wa.await_ready(rt, 2, client, timeout_s=5.0))

    assert client.checks >= 3
    assert client.is_connected


def test_readiness_tolerates_the_bridge_call_failing_while_go_is_nil(tmp_path):
    """`is_connected` can raise, not just return False, before Go is up.

    That is the state being waited out, so it must not abort the wait — which
    would turn a slow connect into an instant failure.
    """
    rt = _rt(tmp_path)
    client = _SlowClient(ready_after=3, raise_until_ready=True)
    asyncio.run(wa.await_ready(rt, 2, client, timeout_s=5.0))
    assert client.is_connected


def test_a_socket_that_never_comes_up_fails_loudly_and_is_audited(tmp_path):
    """Bounded, not infinite: a hung connect must not hang the request."""
    rt = _rt(tmp_path)
    # A real tenant: the audit row carries a tenant_id FK to users(id), so a
    # made-up id would fail the insert and the assertion below would be testing
    # the test rather than the code.
    b_id = _new_tenant(rt, "b@example.com")
    never = _SlowClient(ready_after=10_000)

    with pytest.raises(wa.WhatsAppLinkError, match="did not connect"):
        asyncio.run(wa.await_ready(rt, b_id, never, timeout_s=0.6))

    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_connect_timeout" in actions


def test_readiness_waits_for_connected_not_logged_in(tmp_path):
    """Pairing happens on a connected, NOT-logged-in client.

    Waiting for is_logged_in would deadlock: it cannot become true until the
    user types the code, which they cannot do until we hand them one.
    """
    import inspect

    src = inspect.getsource(wa.await_ready)
    assert "is_connected" in src
    assert "client.is_logged_in" not in src


def test_a_pairing_failure_is_audited_and_drops_the_stale_client(tmp_path):
    """What the live run could not find in the log.

    Every failure — neonize's PairPhoneError included — must land as
    wa_pair_code_failed with the row marked failed, because that note is the
    first place anyone looks. The half-built session is evicted too, so a retry
    does not reuse a client whose Go side never finished connecting.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    evicted: list[tuple] = []

    async def _evict(tenant_id, kind=None):
        evicted.append((tenant_id, kind))
        return 1

    rt.sessions.evict = _evict

    async def _boom(_rt, _tid, _digits):
        raise RuntimeError("PairPhoneError('client is nil')")

    wa.request_pair_code = _boom          # autouse fixture restores it

    with pytest.raises(wa.WhatsAppLinkError, match="could not request"):
        _start(rt, b_id, consent_acknowledged=True)

    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["status"] == "failed"
    assert "client is nil" in row["last_error"]
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_pair_code_failed" in actions
    assert (b_id, "whatsapp") in evicted


def test_a_readiness_timeout_is_also_audited_as_a_pair_failure(tmp_path):
    """The regression this file previously would NOT have caught.

    WhatsAppLinkError used to be re-raised untouched, so a connect timeout left
    the row 'pending' and wrote no wa_pair_code_failed row — which is exactly
    why the live failure looked like it had vanished from the audit log.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    async def _timeout(_rt, _tid, _digits):
        raise wa.WhatsAppLinkError("WhatsApp did not connect within 25s")

    wa.request_pair_code = _timeout

    with pytest.raises(wa.WhatsAppLinkError, match="did not connect"):
        _start(rt, b_id, consent_acknowledged=True)

    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["status"] == "failed"
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_pair_code_failed" in actions


def test_build_client_actually_waits_before_returning(tmp_path):
    """A correct readiness wait that nothing calls is the original bug again.

    Pinned by source order rather than behaviour because standing up a real
    neonize client in a test is the thing we cannot do — and this is precisely
    the seam where "it looks right" already failed once, live.
    """
    import inspect

    src = inspect.getsource(wa.build_client)
    assert "await client.connect()" in src
    assert "await_ready(" in src
    assert src.index("await client.connect()") < src.index("await_ready(")
    # and the result must be returned only after the wait
    assert src.index("await_ready(") < src.rindex("return client")


def test_a_leading_00_international_prefix_is_converted_not_rejected():
    """Much of the world writes 00972… where E.164 writes +972….

    Found by android-app while aligning their client-side validator. It is the
    nastiest shape of input bug: stripping only the '+' leaves 00972… looking
    like a perfectly valid 14-digit number, so it passes the length check and
    fails at WhatsApp with an error the user cannot decode — strictly worse
    than a clean rejection. Unambiguous to detect, because no E.164 country
    code begins with 0.
    """
    assert wa.normalise_phone("00972501234567") == "972501234567"
    assert wa.normalise_phone("00 972 50 123 4567") == "972501234567"
    assert wa.normalise_phone("0097250-123-4567") == "972501234567"
    # and the + form is unaffected
    assert wa.normalise_phone("+972501234567") == "972501234567"


def test_a_bare_00_is_still_refused():
    """Stripping the prefix must not turn junk into an empty 'valid' number."""
    for bad in ("00", "0000", "00 - -"):
        with pytest.raises(wa.PhoneRequired):
            wa.normalise_phone(bad)


@pytest.mark.parametrize("national", [
    "0501234567",        # an Israeli mobile as an Israeli writes it — 10 digits
    "05012345678",
    "0972501234567",     # country code with a stray leading 0
    "000972501234567",   # 00 prefix then a national number
])
def test_a_national_number_is_refused_with_a_usable_hint(national):
    """The gap the length check could never close.

    An earlier version leaned on the 8-digit floor to catch "a local number
    missing its country code" — but that only caught locals short enough to
    trip it. An Israeli mobile is ten digits, comfortably inside 8-15, so it
    passed validation and reached whatsmeow as a bogus number: a plausible
    wrong answer instead of an actionable rejection. Length cannot tell a
    national number from an international one. The leading zero can, because no
    E.164 country code starts with one.
    """
    with pytest.raises(wa.PhoneRequired, match="national number"):
        wa.normalise_phone(national)


def test_the_hint_tells_the_user_what_to_actually_do():
    """A rejection the user cannot act on is barely better than a wrong number."""
    with pytest.raises(wa.PhoneRequired) as exc:
        wa.normalise_phone("0501234567")
    msg = str(exc.value)
    assert "leading 0" in msg and "country code" in msg
    assert "+972501234567" in msg          # a worked example, not just a rule


def test_valid_international_numbers_still_pass():
    """The guard must not start rejecting the numbers it exists to accept."""
    assert wa.normalise_phone("+972501234567") == "972501234567"
    assert wa.normalise_phone("00972501234567") == "972501234567"
    assert wa.normalise_phone("+1 415 555 0123") == "14155550123"
    assert wa.normalise_phone("+44 20 7946 0958") == "442079460958"


# --- parsing is delegated, not hand-rolled -----------------------------------

def test_the_users_real_number_survives_normalisation():
    """The number about to be paired live. Regression floor for any change here.

    Pinned because a stricter validator is exactly the kind of "improvement"
    that would silently lock out the one account we know matters.
    """
    assert wa.normalise_phone("+972555000002") == "972555000002"
    assert wa.normalise_phone("00972555000002") == "972555000002"
    assert wa.normalise_phone("972555000002") == "972555000002"
    assert wa.normalise_phone("+972 55 245 2100") == "972555000002"


def test_well_formed_numbers_in_unassigned_ranges_are_accepted():
    """Deliberately NOT is_valid_number, and this is the reason.

    libphonenumber's carrier-allocation tables lag real allocations: all three
    of these are perfectly well-formed and all three fail is_valid_number. The
    failure modes are not symmetric — a bogus number costs one retry that
    WhatsApp itself rejects, while a wrongly-rejected real number locks that
    user out of linking entirely, with no workaround. So structure is checked,
    not carrier assignment.
    """
    for number in ("+972501234567", "+972521234567", "+35812345678"):
        assert wa.normalise_phone(number) == number.lstrip("+").replace(" ", "")


def test_structurally_impossible_numbers_are_still_refused():
    """is_possible_number is lenient about assignment, not about shape."""
    for bad in ("12345", "+1", "+123"):
        with pytest.raises(wa.PhoneRequired):
            wa.normalise_phone(bad)


def test_parsing_is_delegated_to_phonenumbers():
    """Guards against sliding back into hand-rolled strip rules.

    The hand-rolled version grew one rule per bug report — '+', then '00', then
    a leading '0' — each added only after a user had already hit it. Delegating
    rejects the whole class, including variants nobody has reported yet.
    """
    import inspect

    src = inspect.getsource(wa.normalise_phone)
    assert "phonenumbers.parse" in src
    assert "phonenumbers.is_possible_number" in src
    # The strict check would lock out real users (see the test above). Asserted
    # on the CALL, not the text: the docstring names is_valid_number precisely
    # to explain why it is not used, and a bare substring check cannot tell
    # prose from code.
    assert "phonenumbers.is_valid_number(" not in src


def test_phonenumbers_is_a_declared_dependency():
    """It arrives transitively via neonize, which is not a guarantee.

    A neonize release that dropped it would break linking with an ImportError
    at the worst possible moment, so it is declared directly — same reasoning
    as cryptography via google-auth.
    """
    import pathlib

    pyproject = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
    assert "phonenumbers" in pyproject


# --- SQLITE_READONLY_DBMOVED: deleting session.db under a live client --------

def test_reset_closes_the_client_before_deleting_its_files(tmp_path):
    """The live corruption, in order form.

    whatsmeow opened session.db, a retried /link deleted and recreated it, and
    the next device-store write went to the orphaned inode —
    SQLITE_READONLY_DBMOVED, surfacing as "attempt to write a readonly
    database" AFTER WhatsApp had already accepted the pairing code. The two
    calls look equally reasonable in either order at the call site, so the
    order is asserted rather than trusted.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    order: list[str] = []

    async def _evict(tenant_id, kind=None):
        order.append("evict")
        return 1

    rt.sessions.evict = _evict
    real_wipe = wa.wipe_session

    def _wipe(rt_, tid):
        order.append("wipe")
        return real_wipe(rt_, tid)

    wa.wipe_session = _wipe
    try:
        asyncio.run(wa.reset_session(rt, b_id))
    finally:
        wa.wipe_session = real_wipe

    assert order == ["evict", "wipe"], order


def test_start_link_resets_through_the_safe_helper(tmp_path):
    """A retried /link must not stomp the previous attempt's open file.

    Pinned by source because the failure is invisible in-process: the delete
    succeeds, the recreate succeeds, and only whatsmeow's next write fails —
    minutes later, inside the Go library, after the user has already typed the
    code.
    """
    import inspect

    # Both halves of the entry point: start_link takes the per-tenant lock and
    # _start_link_locked holds it. Checked together so a refactor that moves the
    # body between them cannot make this pass vacuously — the first version of
    # this test read only start_link and went green the moment the body moved.
    src = inspect.getsource(wa.start_link) + inspect.getsource(wa._start_link_locked)
    assert "reset_session(" in src
    # the unsafe pairing must not reappear anywhere on this path
    assert "wipe_session(" not in src


def test_wiping_under_a_live_client_is_audited(tmp_path):
    """A tripwire for the next caller who gets the order wrong.

    It cannot prevent the corruption, but it turns a silent one into a named
    row — the difference between ten minutes of diagnosis and two hours of
    reading /proc/<pid>/fd.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    rt.sessions.peek = lambda tenant_id, kind: object()   # pretend one is live

    wa.wipe_session(rt, b_id)

    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_wipe_under_live_client" in actions


def test_logout_and_unlink_also_close_before_wiping(tmp_path):
    """Same hazard, two other paths that used to wipe directly."""
    import inspect

    for fn in (wa.record_logged_out, wa.unlink):
        src = inspect.getsource(fn)
        assert "reset_session(" in src, fn.__name__
        assert "wipe_session(" not in src, fn.__name__


def test_eviction_prefers_stop_over_disconnect(tmp_path):
    """The other half of the corruption.

    neonize's ``disconnect`` closes the websocket but leaves the Go client
    alive still holding session.db open; only ``stop`` releases it. Evicting
    with ``disconnect`` left the fd on a deleted inode, which is precisely how
    the recreate turned into DBMOVED.
    """
    from archon.sessions import SessionRegistry

    calls: list[str] = []

    class _Client:
        async def stop(self):
            calls.append("stop")

        async def disconnect(self):
            calls.append("disconnect")

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    reg = SessionRegistry()
    reg.register_factory("wa-test", lambda _rt, _tid: _Client())

    asyncio.run(reg.get(rt, b_id, "wa-test"))
    asyncio.run(reg.evict(b_id, "wa-test"))

    assert calls == ["stop"], calls


def test_the_client_is_shut_down_before_the_evict_hook_runs(tmp_path):
    """The hook encrypts session.db back into the DB.

    Running it while the client is still writing reads a file mid-flight — a
    torn read stored as a corrupt session, which would then fail to decrypt or
    fail to log in on the next use.
    """
    from archon.sessions import SessionRegistry

    order: list[str] = []

    class _Client:
        async def stop(self):
            order.append("stop")

    def _hook(_rt, _tenant_id):
        order.append("hook")

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    reg = SessionRegistry()
    reg.register_factory("wa-test", lambda _rt, _tid: _Client(), on_evict=_hook)

    asyncio.run(reg.get(rt, b_id, "wa-test"))
    asyncio.run(reg.evict(b_id, "wa-test"))

    assert order == ["stop", "hook"], order


# --- the readiness gate must ACTUALLY wait -----------------------------------

class _AsyncPropClient:
    """Models neonize's aioze client, where `is_connected` is the trap.

    The property is annotated `-> bool` and documented as returning a bool, but
    in the async client `self.__client` is `async_gocode`, so the body returns
    an unawaited COROUTINE — which is always truthy.
    """

    def __init__(self, ready_after: int = 3) -> None:
        self.checks = 0
        self.ready_after = ready_after

    @property
    def is_connected(self):
        async def _check():
            self.checks += 1
            return self.checks >= self.ready_after
        return _check()          # a coroutine, exactly like neonize's


def test_readiness_awaits_a_coroutine_valued_is_connected(tmp_path):
    """The live regression: the gate passed instantly and waited for nothing.

    `if client.is_connected:` on a coroutine is ALWAYS true, so await_ready
    returned on its first poll every time. PairPhone then fired on an unready
    client and the only trace was a RuntimeWarning in the journal. Neither the
    `-> bool` annotation nor the docstring could be trusted; the value has to be
    inspected.
    """
    rt = _rt(tmp_path)
    client = _AsyncPropClient(ready_after=3)

    asyncio.run(wa.await_ready(rt, 2, client, timeout_s=5.0))

    assert client.checks >= 3, (
        "await_ready returned without waiting — it accepted a truthy coroutine"
    )


def test_a_coroutine_that_stays_false_still_times_out(tmp_path):
    """...and awaiting must not turn 'never ready' into 'instantly ready'."""
    rt = _rt(tmp_path)
    never = _AsyncPropClient(ready_after=10_000)

    with pytest.raises(wa.WhatsAppLinkError, match="did not connect"):
        asyncio.run(wa.await_ready(rt, 2, never, timeout_s=0.6))


def test_readiness_still_works_on_a_plain_bool_client(tmp_path):
    """The sync client returns a real bool; both shapes must work."""
    rt = _rt(tmp_path)

    class _Bool:
        checks = 0

        @property
        def is_connected(self):
            type(self).checks += 1
            return type(self).checks >= 2

    asyncio.run(wa.await_ready(rt, 2, _Bool(), timeout_s=5.0))


def test_no_coroutine_is_left_unawaited(tmp_path, recwarn):
    """The only symptom this bug ever produced was a RuntimeWarning.

    Asserting its absence is what makes the fix observable — the behaviour it
    broke (waiting) is otherwise invisible from outside.
    """
    rt = _rt(tmp_path)
    asyncio.run(wa.await_ready(rt, 2, _AsyncPropClient(ready_after=2), timeout_s=5.0))
    unawaited = [w for w in recwarn
                 if issubclass(w.category, RuntimeWarning)
                 and "never awaited" in str(w.message)]
    assert unawaited == []


# --- one pairing at a time per tenant ----------------------------------------

def test_a_second_link_while_one_is_in_flight_is_refused(tmp_path):
    """Two clients initialising the same session.db is the v0->v14 failure.

    Live, an impatient second tap produced two _connect_and_check tasks failing
    together with "failed to run upgrade v0->v14: attempt to write a readonly
    database" — concurrent schema creation on one file. Refusing beats queuing:
    a queued attempt would hand the user a code from a session the next attempt
    is about to destroy.
    """
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(_rt, _tid, _digits):
        started.set()
        await release.wait()
        return _FAKE_PAIR_CODE

    wa.request_pair_code = _slow

    async def _run():
        first = asyncio.create_task(
            wa.start_link(rt, b_id, consent_acknowledged=True, phone="+972555000002"))
        await started.wait()                      # first is mid-flight
        with pytest.raises(wa.WhatsAppLinkError, match="already in progress"):
            await wa.start_link(rt, b_id, consent_acknowledged=True,
                                phone="+972555000002")
        release.set()
        return await first

    out = asyncio.run(_run())
    assert out["pair_code"] == _FAKE_PAIR_CODE
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_link_already_in_progress" in actions


def test_the_lock_is_released_so_a_later_retry_still_works(tmp_path):
    """A refusal must not wedge the tenant out of ever pairing again."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    first = _start(rt, b_id, consent_acknowledged=True, phone="+972555000002")
    second = _start(rt, b_id, consent_acknowledged=True, phone="+972555000002")
    assert first["status"] == second["status"] == "awaiting_code"


def test_two_tenants_pair_independently(tmp_path):
    """The lock is per tenant — one user linking must not block another."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(_rt, tid, _digits):
        if tid == b_id:
            started.set()
            await release.wait()
        return _FAKE_PAIR_CODE

    wa.request_pair_code = _slow

    async def _run():
        first = asyncio.create_task(
            wa.start_link(rt, b_id, consent_acknowledged=True, phone="+972555000002"))
        await started.wait()
        # c must not be blocked by b's in-flight pairing
        out_c = await wa.start_link(rt, c_id, consent_acknowledged=True,
                                    phone="+14155550123")
        release.set()
        await first
        return out_c

    assert asyncio.run(_run())["status"] == "awaiting_code"
