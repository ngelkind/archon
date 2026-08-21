"""Per-tenant Telegram userbot: consent gate, phone→code→2FA login, encrypted
per-tenant StringSession, isolation and lifecycle.

Telethon is fully mocked — no network, no real account, no code sent.

The consent tests are load-bearing. This integration logs in as the user's own
Telegram account, and Telegram documents that such accounts are placed under
observation and can be banned forever for automated behaviour. The deal is that
the risk is consented, so a regression that let linking start without an
acknowledgement would break it silently.
"""

from __future__ import annotations

import asyncio

import pytest

from archon.crypto import CredentialCryptoError, decrypt
from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope, tenant_purge
from archon.integrations import telegram_userbot as ub

from test_api import _client, _make_device_token, make_rt
from test_tenancy import _new_tenant

_KEY = "a" * 64


def _rt(tmp_path, configured=True):
    rt = make_rt(tmp_path)
    rt.settings.multitenant_enabled = True
    rt.settings.credential_encryption_key = _KEY
    if configured:
        rt.settings.telegram_api_id = 123456
        rt.settings.telegram_api_hash = "abcdef0123456789"
    return rt


# --- the Telethon stand-in ---------------------------------------------------

class SessionPasswordNeededError(Exception):
    """Same NAME as Telethon's; the code detects 2FA by exception type name."""


class _Session:
    def __init__(self, value="SESSION-0"):
        self.value = value

    def save(self):
        return self.value


class FakeTelethon:
    """Records the handshake and can be told to demand 2FA or fail."""

    instances: list["FakeTelethon"] = []

    def __init__(self, rt=None, session=None, *, needs_password=False,
                 send_error=None, signin_error=None, me_id=777,
                 me_username="dana"):
        self.session = _Session(session or "SESSION-0")
        self.calls: list[tuple] = []
        self.needs_password = needs_password
        self.send_error = send_error
        self.signin_error = signin_error
        self._me_id = me_id
        self._me_username = me_username
        self.connected = False
        self.disconnected = False
        FakeTelethon.instances.append(self)

    async def connect(self):
        self.connected = True

    async def send_code_request(self, phone):
        self.calls.append(("send_code", phone))
        if self.send_error:
            raise self.send_error

        class Sent:
            phone_code_hash = "HASH-123"

        self.session.value = "SESSION-AFTER-CODE"
        return Sent()

    async def sign_in(self, phone=None, code=None, phone_code_hash=None,
                      password=None):
        self.calls.append(("sign_in", phone, code, phone_code_hash, password))
        if self.signin_error:
            raise self.signin_error
        if password is None and self.needs_password:
            raise SessionPasswordNeededError("2FA")
        self.session.value = "SESSION-LOGGED-IN"

    async def get_me(self):
        class Me:
            id = self._me_id
            username = self._me_username

        return Me()

    async def disconnect(self):
        self.disconnected = True


def _factory(**kw):
    def make(rt, session=None):
        return FakeTelethon(rt, session, **kw)

    return make


def _start(rt, tenant_id, phone="+447700900000", **kw):
    return asyncio.run(ub.start_login(
        rt, tenant_id, phone=phone, consent_acknowledged=True,
        client_factory=_factory(**kw)))


def _complete(rt, tenant_id, code="12345", password=None, **kw):
    return asyncio.run(ub.complete_login(
        rt, tenant_id, code=code, password=password,
        client_factory=_factory(**kw)))


# --- the consent gate --------------------------------------------------------

def test_login_is_refused_without_consent(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    for bad in (False, None, "yes", 1):
        with pytest.raises(ub.ConsentRequired):
            asyncio.run(ub.start_login(rt, b_id, phone="+447700900000",
                                       consent_acknowledged=bad,
                                       client_factory=_factory()))
    assert repo.tg_userbot_get(TenantScope(rt.db, b_id)) is None
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "tg_userbot_refused_no_consent" in actions


def test_consent_records_the_version_shown(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)

    row = repo.tg_userbot_get(TenantScope(rt.db, b_id))
    assert row["consent_version"] == ub.CONSENT_VERSION
    consent = [dict(r) for r in rt.db.query(
        "SELECT * FROM audit WHERE action = 'tg_userbot_consent_acknowledged'")]
    assert len(consent) == 1
    assert ub.CONSENT_VERSION in consent[0]["detail_json"]


def test_a_stale_consent_version_is_refused(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    with pytest.raises(ub.ConsentRequired, match="updated"):
        asyncio.run(ub.start_login(rt, b_id, phone="+447700900000",
                                   consent_acknowledged=True,
                                   consent_version="1999.v0",
                                   client_factory=_factory()))


def test_the_warning_is_accurate_to_telegrams_actual_position(tmp_path):
    """Telegram WELCOMES third-party clients, so claiming this 'violates
    Telegram's ToS' would be false — and a false warning devalues the WhatsApp
    one, which really does describe a terms breach. It must instead say what
    Telegram documents: observation, and bans for automated abuse."""
    notice = ub.consent_notice()
    warning = notice["warning"].lower()

    assert "under observation" in warning
    assert "banned forever" in warning
    assert "full access" in warning
    # the compliant path is offered alongside
    assert "business bot" in warning
    assert notice["safe_alternative"] == "telegram_business_bot"
    assert notice["reversible"] is False
    # and it must NOT overclaim
    assert "violates telegram's terms" not in warning


def test_api_refuses_to_start_without_acknowledgement(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    headers = {"Authorization": f"Bearer {_make_device_token(rt)}"}

    for body in ({"phone": "+447700900000"},
                 {"phone": "+447700900000", "consent_acknowledged": False}):
        r = client.post("/integrations/telegram/userbot/start", json=body,
                        headers=headers)
        assert r.status_code == 400
    assert repo.tg_userbot_get(TenantScope(rt.db, OWNER_TENANT_ID)) is None


def test_consent_endpoint_serves_the_verbatim_warning(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    headers = {"Authorization": f"Bearer {_make_device_token(rt)}"}
    body = client.get("/integrations/telegram/userbot/consent",
                      headers=headers).json()
    assert body["warning"] == ub.CONSENT_WARNING
    assert body["safe_alternative"] == "telegram_business_bot"


# --- login flow --------------------------------------------------------------

def test_start_sends_the_code_and_records_state(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    FakeTelethon.instances.clear()

    out = _start(rt, b_id, phone="+447700900000")
    assert out["status"] == "code_sent"
    assert ("send_code", "+447700900000") in FakeTelethon.instances[-1].calls
    assert FakeTelethon.instances[-1].disconnected is True

    row = repo.tg_userbot_get(TenantScope(rt.db, b_id))
    assert row["status"] == "code_sent"
    assert row["login_hash_envelope"] is not None
    # the half-finished session is kept: sign-in resumes the same login
    assert row["session_envelope"] is not None


def test_complete_stores_an_encrypted_session(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)
    out = _complete(rt, b_id, code="12345")

    assert out["linked"] is True and out["status"] == "active"
    assert out["username"] == "dana"

    row = repo.tg_userbot_get(TenantScope(rt.db, b_id))
    assert "SESSION-LOGGED-IN" not in row["session_envelope"]   # encrypted
    assert ub.session_string(rt, b_id) == "SESSION-LOGGED-IN"
    # the code hash is spent and cleared, not left lying around
    assert row["login_hash_envelope"] is None


def test_two_factor_password_path(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)

    # without a password the login pauses and says so, rather than failing
    with pytest.raises(ub.PasswordRequired):
        _complete(rt, b_id, code="12345", needs_password=True)
    assert repo.tg_userbot_get(TenantScope(rt.db, b_id))["status"] \
        == "password_required"

    out = _complete(rt, b_id, code="12345", password="hunter2",
                    needs_password=True)
    assert out["linked"] is True
    assert ub.session_string(rt, b_id) == "SESSION-LOGGED-IN"


def test_password_is_never_persisted(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)
    _complete(rt, b_id, code="12345", password="hunter2", needs_password=True)

    dumped = str([dict(r) for r in rt.db.query("SELECT * FROM telegram_userbot_links")])
    assert "hunter2" not in dumped
    audit = str([dict(r) for r in rt.db.query("SELECT * FROM audit")])
    assert "hunter2" not in audit


def test_a_bad_code_fails_without_wiping_consent(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)

    with pytest.raises(ub.UserbotLinkError, match="sign-in failed"):
        _complete(rt, b_id, code="00000",
                  signin_error=RuntimeError("PHONE_CODE_INVALID"))
    row = repo.tg_userbot_get(TenantScope(rt.db, b_id))
    assert row["status"] == "failed"
    assert "PHONE_CODE_INVALID" in row["last_error"]
    assert row["consent_version"] == ub.CONSENT_VERSION    # still recorded


def test_complete_requires_a_login_in_progress(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    with pytest.raises(ub.UserbotLinkError, match="no login in progress"):
        _complete(rt, b_id, code="12345")


def test_phone_must_be_international_format(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    for bad in ("07700900000", "not-a-phone", ""):
        with pytest.raises(ub.UserbotLinkError, match="international format"):
            asyncio.run(ub.start_login(rt, b_id, phone=bad,
                                       consent_acknowledged=True,
                                       client_factory=_factory()))


def test_unconfigured_app_credentials_fail_clearly(tmp_path):
    rt = _rt(tmp_path, configured=False)
    b_id = _new_tenant(rt, "b@example.com")
    assert ub.is_configured(rt) is False
    with pytest.raises(ub.UserbotLinkError, match="TELEGRAM_API_ID"):
        asyncio.run(ub.start_login(rt, b_id, phone="+447700900000",
                                   consent_acknowledged=True,
                                   client_factory=_factory()))


# --- isolation ---------------------------------------------------------------

def test_two_tenants_sessions_are_isolated(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")

    _start(rt, b_id, phone="+447700900001")
    _complete(rt, b_id, code="1")
    _start(rt, c_id, phone="+447700900002")
    _complete(rt, c_id, code="2")

    assert repo.tg_userbot_get(TenantScope(rt.db, b_id))["phone"] == "+447700900001"
    assert repo.tg_userbot_get(TenantScope(rt.db, c_id))["phone"] == "+447700900002"
    # B's envelope cannot be opened as C
    envelope = repo.tg_userbot_get(TenantScope(rt.db, b_id))["session_envelope"]
    with pytest.raises(CredentialCryptoError):
        decrypt(_KEY, envelope, tenant_id=c_id, purpose="telegram_userbot")


def test_a_session_moved_between_tenants_is_unusable(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _start(rt, b_id)
    _complete(rt, b_id, code="1")
    _start(rt, c_id)
    _complete(rt, c_id, code="2")

    stolen = repo.tg_userbot_get(TenantScope(rt.db, b_id))["session_envelope"]
    rt.db.execute("UPDATE telegram_userbot_links SET session_envelope = ? "
                  "WHERE tenant_id = ?", (stolen, c_id))
    assert ub.session_string(rt, c_id) is None      # fails closed
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "tg_userbot_session_undecryptable" in actions


def test_registry_builds_one_client_per_tenant(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    for tid in (b_id, c_id):
        _start(rt, tid)
        _complete(rt, tid, code="1")

    built: list[int] = []

    async def fake_build(rt_, tenant_id):
        built.append(tenant_id)
        return {"tenant": tenant_id,
                "session": ub.session_string(rt_, tenant_id)}

    rt.sessions.register_factory("telegram_userbot", fake_build)

    async def scenario():
        return (await rt.sessions.get(rt, b_id, "telegram_userbot"),
                await rt.sessions.get(rt, c_id, "telegram_userbot"))

    b_client, c_client = asyncio.run(scenario())
    assert b_client is not c_client
    assert built == [b_id, c_id]
    assert rt.sessions.live_count == 2


def test_building_without_a_link_fails_loudly(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    ub.register(rt)
    with pytest.raises(ub.UserbotLinkError, match="no linked Telegram account"):
        asyncio.run(rt.sessions.get(rt, b_id, "telegram_userbot"))


# --- lifecycle ---------------------------------------------------------------

def test_unlink_evicts_and_wipes(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)
    _complete(rt, b_id, code="1")
    rt.sessions.register_factory("telegram_userbot",
                                 lambda rt_, tid: {"t": tid})

    async def scenario():
        await rt.sessions.get(rt, b_id, "telegram_userbot")
        assert rt.sessions.live_count == 1
        return await ub.unlink(rt, b_id)

    assert asyncio.run(scenario()) is True
    assert rt.sessions.live_count == 0
    assert repo.tg_userbot_get(TenantScope(rt.db, b_id)) is None
    assert ub.session_string(rt, b_id) is None


def test_a_ban_is_recorded_distinctly(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)
    _complete(rt, b_id, code="1")

    ub.record_logged_out(rt, b_id, banned=True)
    scope = TenantScope(rt.db, b_id)
    assert ub.status(rt, scope)["status"] == "banned"
    assert repo.tg_userbot_get(scope)["session_envelope"] is None
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "tg_userbot_banned" in actions


def test_relinking_revokes_the_previous_session(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id, phone="+447700900001")
    _complete(rt, b_id, code="1")
    _start(rt, b_id, phone="+447700900002")

    row = repo.tg_userbot_get(TenantScope(rt.db, b_id))
    assert row["phone"] == "+447700900002" and row["status"] == "code_sent"
    assert len(rt.db.query("SELECT 1 FROM telegram_userbot_links WHERE tenant_id = ?",
                           (b_id,))) == 2       # consent history retained


def test_purging_a_tenant_removes_the_userbot_link(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _start(rt, b_id)
    _complete(rt, b_id, code="1")
    tenant_purge(rt.db, b_id)
    assert rt.db.query("SELECT 1 FROM telegram_userbot_links WHERE tenant_id = ?",
                       (b_id,)) == []


def test_active_tenants_lists_only_logged_in_ones(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _start(rt, b_id)
    _complete(rt, b_id, code="1")
    _start(rt, c_id)                                   # code sent, not signed in

    assert [r["tenant_id"] for r in repo.tg_userbot_active_tenants(rt.db)] == [b_id]


# --- API ---------------------------------------------------------------------

def test_status_and_unlink_endpoints(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    headers = {"Authorization": f"Bearer {_make_device_token(rt)}"}

    assert client.get("/integrations/telegram/userbot",
                      headers=headers).json()["status"] == "not_linked"

    _start(rt, OWNER_TENANT_ID)
    _complete(rt, OWNER_TENANT_ID, code="1")
    body = client.get("/integrations/telegram/userbot", headers=headers).json()
    assert body["linked"] is True and body["username"] == "dana"

    assert client.delete("/integrations/telegram/userbot",
                         headers=headers).status_code == 200
    assert client.get("/integrations/telegram/userbot",
                      headers=headers).json()["status"] == "not_linked"


def test_userbot_endpoints_require_auth(tmp_path):
    rt = _rt(tmp_path)
    client = _client(rt)
    assert client.get("/integrations/telegram/userbot/consent").status_code == 401
    assert client.post("/integrations/telegram/userbot/start",
                       json={"phone": "+4477", "consent_acknowledged": True}
                       ).status_code == 401
    assert client.get("/integrations/telegram/userbot").status_code == 401


def test_business_bot_integration_is_untouched(tmp_path):
    """The compliant path stays available alongside the userbot."""
    from archon.integrations import telegram as tg_business

    rt = _rt(tmp_path)
    rt.settings.telegram_bot_username = "ArchonProductBot"
    b_id = _new_tenant(rt, "b@example.com")
    start = tg_business.start_link(rt, b_id)
    assert start["deep_link"].startswith("https://t.me/ArchonProductBot")
