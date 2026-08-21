"""Per-tenant Telegram: the link handshake, Business-connection routing, and
the Bot API front door.

No network — aiogram objects are simple stubs, so the whole path from "Telegram
delivered an update" to "it was filed under the right tenant" runs in-process.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope, owner_scope
from archon.integrations import telegram as tg
from archon.platforms.telegram import business

from test_api import make_rt
from test_tenancy import _new_tenant


def _rt(tmp_path, multitenant=True):
    rt = make_rt(tmp_path)
    rt.settings.multitenant_enabled = multitenant
    rt.settings.telegram_bot_username = "ArchonProductBot"
    rt.settings.credential_encryption_key = "a" * 64
    return rt


def _link(rt, tenant_id, tg_user_id, username="user"):
    start = tg.start_link(rt, tenant_id)
    return tg.complete_link(rt, code=start["code"], tg_user_id=str(tg_user_id),
                            tg_username=username, tg_name=username.title())


# --- the link handshake ------------------------------------------------------

def test_link_code_binds_a_telegram_account_to_a_tenant(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")

    start = tg.start_link(rt, b_id)
    assert start["deep_link"] == f"https://t.me/ArchonProductBot?start={start['code']}"
    assert len(start["code"]) == 8

    assert tg.complete_link(rt, code=start["code"], tg_user_id="555",
                            tg_username="bee") == b_id
    assert tg.tenant_for_user(rt, "555") == b_id
    assert tg.status(TenantScope(rt.db, b_id))["linked"] is True
    assert tg.status(TenantScope(rt.db, b_id))["connected"] is False


def test_link_codes_are_single_use_and_expire(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    start = tg.start_link(rt, b_id)

    tg.complete_link(rt, code=start["code"], tg_user_id="555")
    with pytest.raises(tg.TelegramLinkError, match="already-used"):
        tg.complete_link(rt, code=start["code"], tg_user_id="666")
    with pytest.raises(tg.TelegramLinkError):
        tg.complete_link(rt, code="NEVERISSUED", tg_user_id="777")

    # an expired code is refused too
    from archon.api.security import hash_secret

    repo.telegram_link_code_create(
        rt.db, code_hash=hash_secret(rt.settings.api_token_pepper, "STALE123"),
        tenant_id=b_id, expires_at="2000-01-01 00:00:00")
    with pytest.raises(tg.TelegramLinkError):
        tg.complete_link(rt, code="STALE123", tg_user_id="888")


def test_codes_are_stored_only_as_hashes(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    code = tg.start_link(rt, b_id)["code"]
    rows = rt.db.query("SELECT * FROM telegram_link_codes")
    assert code not in str([dict(r) for r in rows])


def test_relinking_a_different_account_revokes_the_old_one(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    _link(rt, b_id, "999", username="newphone")

    assert tg.tenant_for_user(rt, "999") == b_id
    assert tg.tenant_for_user(rt, "555") is None      # old binding is dead
    live = rt.db.query("SELECT * FROM telegram_links WHERE revoked_at IS NULL")
    assert len(live) == 1 and live[0]["tg_user_id"] == "999"


def test_two_tenants_cannot_share_one_telegram_account(tmp_path):
    """The routing key must resolve to exactly one tenant, or a private chat
    could be filed under the wrong account."""
    import sqlite3

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, b_id, "555")
    with pytest.raises(sqlite3.IntegrityError):
        repo.telegram_link_upsert(TenantScope(rt.db, c_id), tg_user_id="555")


# --- Business connection routing ---------------------------------------------

class FakeUser:
    def __init__(self, uid, username="u", full_name="U"):
        self.id = uid
        self.username = username
        self.full_name = full_name


class FakeConnection:
    def __init__(self, conn_id, user, is_enabled=True):
        self.id = conn_id
        self.user = user
        self.is_enabled = is_enabled


def test_connection_update_routes_to_the_linked_tenant(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")

    assert tg.record_connection(rt, tg_user_id="555",
                                business_connection_id="conn-B", enabled=True) == b_id
    assert tg.tenant_for_connection(rt, "conn-B") == b_id
    status = tg.status(TenantScope(rt.db, b_id))
    assert status["connected"] is True and status["tg_username"] == "user"


def test_connection_from_an_unlinked_account_is_refused(tmp_path):
    rt = _rt(tmp_path)
    assert tg.record_connection(rt, tg_user_id="nobody",
                                business_connection_id="conn-X", enabled=True) is None
    assert tg.tenant_for_connection(rt, "conn-X") is None
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "telegram_connection_unlinked_user" in actions


def test_disconnecting_clears_the_routing_key(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True)

    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=False)
    assert tg.tenant_for_connection(rt, "conn-B") is None
    assert tg.status(TenantScope(rt.db, b_id))["connected"] is False
    assert tg.connection_id_for(rt, TenantScope(rt.db, b_id)) is None


def test_two_tenants_connections_route_independently(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, b_id, "555")
    _link(rt, c_id, "666")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True)
    tg.record_connection(rt, tg_user_id="666", business_connection_id="conn-C",
                         enabled=True)

    assert tg.tenant_for_connection(rt, "conn-B") == b_id
    assert tg.tenant_for_connection(rt, "conn-C") == c_id
    assert tg.connection_id_for(rt, TenantScope(rt.db, b_id)) == "conn-B"
    assert tg.connection_id_for(rt, TenantScope(rt.db, c_id)) == "conn-C"


def test_resolve_tenant_drops_unknown_connections_in_product_mode(tmp_path):
    """An update we cannot attribute is dropped, never guessed — guessing would
    file one person's private chat under another's account."""
    rt = _rt(tmp_path, multitenant=True)
    assert business._resolve_tenant(rt, "conn-unknown") is None
    assert business._resolve_tenant(rt, None) is None

    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True)
    assert business._resolve_tenant(rt, "conn-B") == b_id


def test_single_user_mode_always_routes_to_the_owner(tmp_path):
    """The personal bot has exactly one connector; behaviour is unchanged."""
    rt = _rt(tmp_path, multitenant=False)
    assert business._resolve_tenant(rt, "anything") == OWNER_TENANT_ID
    assert business._resolve_tenant(rt, None) == OWNER_TENANT_ID


# --- inbound stamping --------------------------------------------------------

class FakeChat:
    def __init__(self, cid):
        self.id = cid
        self.first_name = "Dana"
        self.last_name = None
        self.title = None
        self.username = "dana"


class FakeMessage:
    def __init__(self, chat_id, msg_id, text, sender, conn_id):
        self.chat = FakeChat(chat_id)
        self.message_id = msg_id
        self.text = text
        self.caption = None
        self.from_user = sender
        self.date = datetime.now(UTC)
        self.business_connection_id = conn_id
        self.photo = None


def test_inbound_is_stamped_with_the_routed_tenant(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True)

    msg = FakeMessage(42, 7, "hi there", FakeUser(999, "dana", "Dana"), "conn-B")
    inbound = business._to_inbound(rt, msg, tenant_id=b_id)
    assert inbound.tenant_id == b_id
    assert inbound.is_from_me is False          # sent BY the correspondent


def test_is_from_me_uses_the_tenants_own_telegram_account(tmp_path):
    """Business updates carry both directions; 'from me' must mean the TENANT,
    not the process owner — otherwise a user's own replies look like incoming
    mail from a stranger."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")

    own = FakeMessage(42, 8, "my reply", FakeUser(555), "conn-B")
    assert business._to_inbound(rt, own, tenant_id=b_id).is_from_me is True

    # the process owner's id is NOT this tenant's identity
    owner_msg = FakeMessage(42, 9, "x",
                            FakeUser(rt.settings.telegram_owner_id), "conn-B")
    assert business._to_inbound(rt, owner_msg, tenant_id=b_id).is_from_me is False
    # ...and for tenant 1 it still is
    assert business._to_inbound(rt, owner_msg,
                                tenant_id=OWNER_TENANT_ID).is_from_me is True


# --- send side ---------------------------------------------------------------

def test_sends_use_the_callers_own_business_connection(tmp_path):
    from archon.tools.telegram import _business_connection_id

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, b_id, "555")
    _link(rt, c_id, "666")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True)
    tg.record_connection(rt, tg_user_id="666", business_connection_id="conn-C",
                         enabled=True)

    assert _business_connection_id(rt, TenantScope(rt.db, b_id)) == "conn-B"
    assert _business_connection_id(rt, TenantScope(rt.db, c_id)) == "conn-C"

    # the single-user owner still uses the legacy setting
    repo.setting_set(owner_scope(rt.db), "tg.business_connection_id", "legacy-conn")
    assert _business_connection_id(rt, owner_scope(rt.db)) == "legacy-conn"


# --- unlink ------------------------------------------------------------------

def test_unlink_stops_routing_and_sending(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True)

    assert tg.unlink(rt, b_id) is True
    assert tg.tenant_for_connection(rt, "conn-B") is None
    assert tg.tenant_for_user(rt, "555") is None
    assert tg.status(TenantScope(rt.db, b_id)) == {"linked": False, "connected": False}


def test_purging_a_tenant_removes_their_telegram_link(tmp_path):
    from archon.db.tenancy import tenant_purge

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tenant_purge(rt.db, b_id)
    assert tg.tenant_for_user(rt, "555") is None


# --- API ---------------------------------------------------------------------

def _client_and_headers(rt):
    from test_api import _client, _make_device_token

    return _client(rt), {"Authorization": f"Bearer {_make_device_token(rt)}"}


def test_link_endpoint_issues_a_code_for_the_calling_tenant(tmp_path):
    rt = _rt(tmp_path)
    client, headers = _client_and_headers(rt)

    body = client.post("/integrations/telegram/link", headers=headers).json()
    assert body["deep_link"].startswith("https://t.me/ArchonProductBot?start=")
    assert body["expires_in_minutes"] == 15
    # the device was created under the owner tenant, so the code is bound to it
    assert tg.complete_link(rt, code=body["code"],
                            tg_user_id="123") == OWNER_TENANT_ID


def test_status_and_unlink_endpoints(tmp_path):
    rt = _rt(tmp_path)
    client, headers = _client_and_headers(rt)

    assert client.get("/integrations/telegram", headers=headers).json() == {
        "linked": False, "connected": False, "tg_username": None,
        "tg_name": None, "linked_at": None, "connected_at": None}

    _link(rt, OWNER_TENANT_ID, "555", username="owner")
    status = client.get("/integrations/telegram", headers=headers).json()
    assert status["linked"] is True and status["tg_username"] == "owner"

    assert client.delete("/integrations/telegram", headers=headers).status_code == 200
    assert client.get("/integrations/telegram",
                      headers=headers).json()["linked"] is False


def test_telegram_endpoints_require_auth(tmp_path):
    from test_api import _client

    rt = _rt(tmp_path)
    client = _client(rt)
    assert client.post("/integrations/telegram/link").status_code == 401
    assert client.get("/integrations/telegram").status_code == 401


# --- the connection id is a secret, not an identifier ------------------------

def test_connection_id_is_never_stored_in_the_clear(tmp_path):
    """Whoever holds a business_connection_id can send as that user, so it is
    encrypted at rest and only a hash is indexed for routing."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-SECRET",
                         enabled=True)

    rows = rt.db.query("SELECT * FROM telegram_links")
    blob = str([dict(r) for r in rows])
    assert "conn-SECRET" not in blob
    # ...but it round-trips for the tenant that owns it
    assert tg.connection_id_for(rt, TenantScope(rt.db, b_id)) == "conn-SECRET"


def test_another_tenant_cannot_decrypt_the_connection(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, b_id, "555")
    _link(rt, c_id, "666")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True)

    # move B's envelope onto C's row, as a stolen-DB attacker might
    envelope = rt.db.query_one(
        "SELECT secret_envelope FROM telegram_links WHERE tenant_id = ?",
        (b_id,))["secret_envelope"]
    rt.db.execute(
        "UPDATE telegram_links SET secret_envelope = ?, is_enabled = 1 "
        "WHERE tenant_id = ?", (envelope, c_id))
    assert tg.connection_id_for(rt, TenantScope(rt.db, c_id)) is None
    actions = [r["action"] for r in rt.db.query("SELECT action FROM audit")]
    assert "telegram_connection_undecryptable" in actions


# --- BusinessBotRights -------------------------------------------------------

def test_send_is_refused_when_reply_permission_was_not_granted(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True, rights={"can_reply": False,
                                               "can_read_messages": True})
    store = TenantScope(rt.db, b_id)
    assert tg.granted_rights(store) == {"can_reply": False,
                                        "can_read_messages": True}
    assert tg.can_reply(store) is False


def test_reply_is_allowed_when_granted_or_unspecified(tmp_path):
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _link(rt, b_id, "555")
    tg.record_connection(rt, tg_user_id="555", business_connection_id="conn-B",
                         enabled=True, rights={"can_reply": True})
    assert tg.can_reply(TenantScope(rt.db, b_id)) is True

    # an older client sending no rights object must not be treated as a denial
    c_id = _new_tenant(rt, "c@example.com")
    _link(rt, c_id, "666")
    tg.record_connection(rt, tg_user_id="666", business_connection_id="conn-C",
                         enabled=True, rights=None)
    assert tg.can_reply(TenantScope(rt.db, c_id)) is True


def test_rights_are_normalised_from_either_bot_api_shape(tmp_path):
    from archon.platforms.telegram.business import _rights_dict

    class Rights:
        can_reply = True
        can_read_messages = False

    class NewStyle:
        rights = Rights()

    class OldStyle:
        rights = None
        can_reply = False

    assert _rights_dict(NewStyle()) == {"can_reply": True,
                                        "can_read_messages": False}
    assert _rights_dict(OldStyle()) == {"can_reply": False}


# --- the 24-hour reply window ------------------------------------------------

def test_reply_window_blocks_proactive_sends(tmp_path):
    """Telegram forbids messaging first; we enforce it locally so the agent gets
    an explanation instead of an opaque API error."""
    from archon.models import InboundMessage

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    store = TenantScope(rt.db, b_id)
    chat_pk = repo.chat_upsert(store, "tg", "42", "Dana", "private")

    # nobody has written to us in this chat -> no window at all
    with pytest.raises(tg.ReplyWindowExpired, match="not written"):
        tg.check_reply_window(store, chat_pk)

    def _cache(hours_ago: float) -> None:
        from datetime import timedelta

        when = datetime.now(UTC) - timedelta(hours=hours_ago)
        repo.message_upsert(store, InboundMessage(
            platform="tg", source="business", chat_id="42", chat_kind="private",
            msg_id=f"m{hours_ago}", sender_id="dana", ts=when, text="hi",
            tenant_id=b_id), chat_pk)

    _cache(30)                                  # stale: window closed
    with pytest.raises(tg.ReplyWindowExpired, match="window has closed"):
        tg.check_reply_window(store, chat_pk)

    _cache(1)                                   # fresh: allowed
    tg.check_reply_window(store, chat_pk)


def test_reply_window_ignores_our_own_messages(tmp_path):
    """Sending does not re-open the window — only the other person writing does,
    otherwise one reply would let us message forever."""
    from datetime import timedelta

    from archon.models import InboundMessage

    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    store = TenantScope(rt.db, b_id)
    chat_pk = repo.chat_upsert(store, "tg", "42", "Dana", "private")

    repo.message_upsert(store, InboundMessage(
        platform="tg", source="business", chat_id="42", chat_kind="private",
        msg_id="theirs", sender_id="dana", ts=datetime.now(UTC) - timedelta(hours=40),
        text="old", tenant_id=b_id), chat_pk)
    repo.message_cache_outgoing(store, chat_pk=chat_pk, platform="tg",
                                chat_id="42", msg_id="mine", source="business",
                                text="just sent")

    with pytest.raises(tg.ReplyWindowExpired):
        tg.check_reply_window(store, chat_pk)


def test_reply_window_is_skipped_when_there_is_no_chat(tmp_path):
    """A brand-new outbound (no cached chat) is left to Telegram to judge."""
    rt = _rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    tg.check_reply_window(TenantScope(rt.db, b_id), None)


# --- the product bot is its own bot ------------------------------------------

def test_product_bot_only_runs_under_the_flag_with_a_token(tmp_path):
    from archon.platforms.telegram import product

    rt = _rt(tmp_path, multitenant=False)
    rt.settings.product_telegram_bot_token = "123:abc"
    assert product.enabled(rt) is False          # flag off

    rt.settings.multitenant_enabled = True
    assert product.enabled(rt) is True

    rt.settings.product_telegram_bot_token = ""
    assert product.enabled(rt) is False          # no separate token


def test_control_bot_no_longer_hosts_product_handlers():
    """The owner's dispatcher must be the owner's alone."""
    import inspect

    from archon.platforms.telegram import control

    src = inspect.getsource(control)
    assert "product.register" not in src
