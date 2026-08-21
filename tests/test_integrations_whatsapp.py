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


def _linked(rt, tenant_id, session_bytes=b"session-blob"):
    """A tenant who consented, paired, and has a session on disk."""
    wa.start_link(rt, tenant_id, consent_acknowledged=True)
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
            wa.start_link(rt, b_id, consent_acknowledged=bad)

    # nothing was created: no link row, no session, no directory
    assert repo.whatsapp_link_get(TenantScope(rt.db, b_id)) is None
    assert not wa.session_path(rt, b_id).exists()


def test_refusal_is_audited(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    with pytest.raises(wa.ConsentRequired):
        wa.start_link(rt, b_id, consent_acknowledged=False)
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_link_refused_no_consent" in actions


def test_consent_is_recorded_with_the_version_that_was_shown(tmp_path):
    """A consent record that does not say WHAT was agreed to is worthless once
    the wording changes."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    wa.start_link(rt, b_id, consent_acknowledged=True)

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
        wa.start_link(rt, b_id, consent_acknowledged=True,
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

    ok = client.post("/integrations/whatsapp/link",
                     json={"consent_acknowledged": True}, headers=headers)
    assert ok.status_code == 200 and ok.json()["status"] == "pending"


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
    wa.start_link(rt, b_id, consent_acknowledged=True)
    assert wa.status(rt, TenantScope(rt.db, b_id))["status"] == "pending"

    wa.session_path(rt, b_id).write_bytes(b"s")
    wa.record_pair_status(rt, b_id, ok=True, phone_jid="4477@s.whatsapp.net")
    st = wa.status(rt, TenantScope(rt.db, b_id))
    assert st["linked"] is True and st["phone"] == "4477@s.whatsapp.net"

    c_id = _new_tenant(rt, "c@example.com")
    wa.start_link(rt, c_id, consent_acknowledged=True)
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

    wa.record_logged_out(rt, b_id, banned=True)
    assert wa.status(rt, TenantScope(rt.db, b_id))["status"] == "banned"
    assert not wa.session_path(rt, b_id).exists()
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "wa_banned" in actions and "wa_logged_out" not in actions


def test_relinking_replaces_the_previous_session(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _linked(rt, b_id, b"OLD")
    wa.persist_session(rt, b_id, wipe=True)

    wa.start_link(rt, b_id, consent_acknowledged=True)
    row = repo.whatsapp_link_get(TenantScope(rt.db, b_id))
    assert row["status"] == "pending"
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
    wa.start_link(rt, c_id, consent_acknowledged=True)     # pending, not paired

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
