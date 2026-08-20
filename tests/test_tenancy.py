"""Multi-tenancy: isolation, the scoping mechanism, the session registry, and
the fail-closed production checks.

The load-bearing test here is ``test_cross_tenant_isolation``: it seeds two
tenants with deliberately COLLIDING data (same WhatsApp group, same contact,
same persona name, same setting key) and asserts neither can see or modify the
other's rows. It is mutation-tested in ``test_isolation_test_would_catch_a_leak``
— if the tenant predicate is dropped from a query, the assertions fail.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from archon.config import InsecureConfigError, Settings, check_production_secrets
from archon.db import repo
from archon.db.tenancy import (
    GLOBAL_TABLES, OWNER_TENANT_ID, TENANTED_TABLES, TenantScope,
    TenantScopeError, as_scope, owner_scope, tenant_purge,
)
from archon.sessions import SessionRegistry
from archon.tenant import owner_context, tenant_context

from test_api import make_rt


def _new_tenant(rt, email: str) -> int:
    return repo.user_create(rt.db, email=email, password_hash="x", display_name=None)


def _seed(rt, tenant_id: int, tag: str) -> dict:
    """Give a tenant a full set of rows, deliberately colliding with the other
    tenant's on every globally-unique key the old schema had."""
    sc = TenantScope(rt.db, tenant_id)
    chat_pk = repo.chat_upsert(sc, "wa", "shared@g.us", f"Family-{tag}", "group")
    repo.setting_set(sc, "llm.active_provider", tag)
    repo.contact_add(sc, "Dana", "+972500000000", "dana", "import")
    repo.persona_upsert(sc, "work", f"prompt-{tag}")
    repo.gmail_state_set(sc, "historyId", tag)
    repo.context_add(sc, chat_pk, None, "user", f"secret-{tag}")
    repo.llm_call_record(sc, purpose="agent", provider="gemini", model="m",
                         cost_usd=1.0)
    action_id = repo.pending_action_create(
        sc, kind="wa.send", payload_json="{}", chat_pk=chat_pk,
        expires_at="2099-01-01 00:00:00")
    sched_id = repo.schedule_create(sc, platform="wa", chat_pk=chat_pk,
                                    text=f"sched-{tag}", due_at="2099-01-01 00:00:00")
    device_id = repo.api_device_create(sc, name=f"phone-{tag}",
                                       token_hash=f"hash-{tag}")
    return {"scope": sc, "chat_pk": chat_pk, "action_id": action_id,
            "sched_id": sched_id, "device_id": device_id}


# --- the isolation guarantee -------------------------------------------------

def test_cross_tenant_isolation(tmp_path):
    rt = make_rt(tmp_path)
    a_id = OWNER_TENANT_ID                      # tenant 1 exists from migration 008
    b_id = _new_tenant(rt, "b@example.com")
    a = _seed(rt, a_id, "A")
    b = _seed(rt, b_id, "B")
    sa, sb = a["scope"], b["scope"]

    # --- reads: each tenant sees exactly its own row of each colliding pair ---
    assert [c["name"] for c in repo.chat_list(sa)] == ["Family-A"]
    assert [c["name"] for c in repo.chat_list(sb)] == ["Family-B"]
    assert repo.setting_get(sa, "llm.active_provider") == "A"
    assert repo.setting_get(sb, "llm.active_provider") == "B"
    assert repo.gmail_state_get(sa, "historyId") == "A"
    assert repo.gmail_state_get(sb, "historyId") == "B"
    assert repo.persona_by_name(sa, "work")["system_prompt"] == "prompt-A"
    assert repo.persona_by_name(sb, "work")["system_prompt"] == "prompt-B"
    assert len(repo.contact_list(sa)) == 1 and len(repo.contact_list(sb)) == 1
    assert len(repo.persona_list(sa)) == 1 and len(repo.persona_list(sb)) == 1

    # --- B cannot reach A's rows by id, even knowing the id ------------------
    assert repo.chat_get_by_pk(sb, a["chat_pk"]) is None
    assert repo.chat_get_by_pk(sa, a["chat_pk"]) is not None
    assert repo.pending_action_get(sb, a["action_id"]) is None
    assert repo.message_history(sb, a["chat_pk"]) == []
    assert repo.context_get(sb, a["chat_pk"], None) == []
    assert [r["content"] for r in repo.context_get(sa, a["chat_pk"], None)] \
        == ["secret-A"]

    # --- listings never bleed ------------------------------------------------
    assert [r["id"] for r in repo.pending_action_list(sa)] == [a["action_id"]]
    assert [r["id"] for r in repo.pending_action_list(sb)] == [b["action_id"]]
    assert [r["id"] for r in repo.schedule_list(sa)] == [a["sched_id"]]
    assert [r["id"] for r in repo.api_device_list(sb)] == [b["device_id"]]
    assert repo.llm_cost_since(sa, "-1 day")["calls"] == 1  # not 2

    # --- writes: B's attempts on A's rows are no-ops, not silent edits -------
    repo.chat_set_field(sb, a["chat_pk"], "is_whitelisted", 1)
    assert repo.chat_get_by_pk(sa, a["chat_pk"])["is_whitelisted"] == 0

    assert repo.pending_action_claim(sb, a["action_id"], "approved") is False
    assert repo.pending_action_get(sa, a["action_id"])["status"] == "pending"

    assert repo.schedule_cancel(sb, a["sched_id"]) is False
    assert repo.schedule_list(sa)[0]["status"] == "pending"

    repo.api_device_revoke(sb, a["device_id"])
    assert repo.api_device_list(sa)[0]["revoked_at"] is None

    repo.context_clear(sb, a["chat_pk"], None)
    assert len(repo.context_get(sa, a["chat_pk"], None)) == 1

    # --- purging B entirely leaves A intact ---------------------------------
    deleted = tenant_purge(rt.db, b_id)
    assert deleted["chats"] == 1 and deleted["settings"] == 1
    assert repo.chat_list(sb) == [] and repo.contact_list(sb) == []
    assert [c["name"] for c in repo.chat_list(sa)] == ["Family-A"]
    assert repo.setting_get(sa, "llm.active_provider") == "A"
    assert repo.llm_cost_since(sa, "-1 day")["calls"] == 1


def test_purge_refuses_to_delete_the_owner_tenant(tmp_path):
    """The owner tenant is the personal bot's own data — never purgeable."""
    rt = make_rt(tmp_path)
    _seed(rt, OWNER_TENANT_ID, "A")
    with pytest.raises(ValueError, match="owner tenant"):
        tenant_purge(rt.db, OWNER_TENANT_ID)
    assert len(repo.chat_list(owner_scope(rt.db))) == 1


def test_isolation_test_would_catch_a_leak(tmp_path, monkeypatch):
    """Mutation test: strip the tenant predicate and the isolation assertions
    must start failing. Without this, a scoping bug could silently make the
    isolation test vacuous."""
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    a = _seed(rt, OWNER_TENANT_ID, "A")
    _seed(rt, b_id, "B")
    sb = TenantScope(rt.db, b_id)

    # Sanity: with scoping intact, B cannot see A's chat.
    assert repo.chat_get_by_pk(sb, a["chat_pk"]) is None

    def leaky_chat_get_by_pk(store, pk):
        return as_scope(store).db.query_one("SELECT * FROM chats WHERE id = ?", (pk,))

    monkeypatch.setattr(repo, "chat_get_by_pk", leaky_chat_get_by_pk)
    # The same assertion the isolation test makes now FAILS -> it is load-bearing.
    assert repo.chat_get_by_pk(sb, a["chat_pk"]) is not None


# --- the scoping mechanism ---------------------------------------------------

def test_scope_requires_a_real_tenant():
    class _FakeDb:
        pass

    for bad in (0, -1, True, None, "1", 1.0):
        with pytest.raises((ValueError, TypeError)):
            TenantScope(_FakeDb(), bad)  # type: ignore[arg-type]


def test_raw_db_resolves_to_the_owner_tenant(tmp_path):
    rt = make_rt(tmp_path)
    assert as_scope(rt.db).tenant_id == OWNER_TENANT_ID
    assert owner_scope(rt.db).tenant_id == OWNER_TENANT_ID
    # a scope passes through unchanged
    sc = TenantScope(rt.db, 1)
    assert as_scope(sc) is sc


def test_tripwire_rejects_unscoped_sql_on_a_tenanted_table(tmp_path):
    rt = make_rt(tmp_path)
    sc = TenantScope(rt.db, OWNER_TENANT_ID)

    with pytest.raises(TenantScopeError, match="chats"):
        sc.query("SELECT * FROM chats")
    with pytest.raises(TenantScopeError):
        sc.execute("UPDATE messages SET text = 'x'")
    # global tables are unaffected
    sc.query("SELECT * FROM users")
    # and a properly-scoped statement runs
    sc.query("SELECT * FROM chats WHERE tenant_id = ?", (sc.tenant_id,))


def test_tenanted_table_list_matches_the_schema(tmp_path):
    """TENANTED_TABLES drives the tripwire, so it must not drift from the DB."""
    rt = make_rt(tmp_path)
    actual = set()
    for row in rt.db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        cols = {c["name"] for c in rt.db.query(f"PRAGMA table_info({row['name']})")}
        if "tenant_id" in cols:
            actual.add(row["name"])
    assert actual == set(TENANTED_TABLES)
    assert not (TENANTED_TABLES & GLOBAL_TABLES)


def test_forgetting_the_tenant_fails_loudly(tmp_path):
    """The DEFAULT 0 tripwire: a write that omits tenant_id must raise, not land
    silently in the owner's data."""
    rt = make_rt(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        rt.db.execute(
            "INSERT INTO llm_calls (purpose, provider, model) VALUES ('x','y','z')"
        )


# --- Runtime split / TenantContext ------------------------------------------

def test_owner_context_reuses_the_existing_control_chat(tmp_path):
    """The personal bot's memory thread must survive tenancy untouched."""
    rt = make_rt(tmp_path)
    owner = owner_context(rt)
    assert owner.is_owner
    pk = owner.control_chat_pk()
    row = repo.chat_get_by_pk(owner.scope, pk)
    assert row["platform"] == "tg"
    assert row["chat_id"] == str(rt.settings.telegram_owner_id)


def test_tenants_get_separate_minds(tmp_path):
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    a_pk = owner_context(rt).control_chat_pk()
    b_pk = tenant_context(rt, b_id).control_chat_pk()
    assert a_pk != b_pk

    # ...and separate agent memory in those chats
    repo.context_add(TenantScope(rt.db, OWNER_TENANT_ID), a_pk, None, "user", "mine")
    assert repo.context_get(TenantScope(rt.db, b_id), b_pk, None) == []


def test_tenant_settings_are_independent(tmp_path):
    rt = make_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    a, b = owner_context(rt), tenant_context(rt, b_id)
    a.set_setting("llm.daily_budget_usd", 3.0)
    b.set_setting("llm.daily_budget_usd", 99.0)
    assert a.setting("llm.daily_budget_usd") == 3.0
    assert b.setting("llm.daily_budget_usd") == 99.0
    assert b.setting("never.set", "fallback") == "fallback"


# --- session registry --------------------------------------------------------

def test_sessions_are_per_tenant_and_lazy(tmp_path):
    rt = make_rt(tmp_path)
    built: list[int] = []

    def factory(rt_, tenant_id):
        built.append(tenant_id)
        return {"tenant": tenant_id}

    rt.sessions.register_factory("whatsapp", factory)

    async def scenario():
        a = await rt.sessions.get(rt, 1, "whatsapp")
        b = await rt.sessions.get(rt, 2, "whatsapp")
        again = await rt.sessions.get(rt, 1, "whatsapp")
        return a, b, again

    a, b, again = asyncio.run(scenario())
    assert a["tenant"] == 1 and b["tenant"] == 2
    assert a is again                      # cached, not rebuilt
    assert built == [1, 2]                 # built once per tenant
    assert rt.sessions.live_count == 2
    assert rt.sessions.peek(1, "whatsapp") is a
    assert rt.sessions.peek(3, "whatsapp") is None


def test_session_eviction_closes_the_client(tmp_path):
    rt = make_rt(tmp_path)
    closed: list[int] = []

    class Client:
        def __init__(self, tenant_id):
            self.tenant_id = tenant_id

        def close(self):
            closed.append(self.tenant_id)

    rt.sessions.register_factory("telegram", lambda rt_, tid: Client(tid))

    async def scenario():
        await rt.sessions.get(rt, 1, "telegram")
        await rt.sessions.get(rt, 2, "telegram")
        dropped = await rt.sessions.evict(1)
        return dropped

    assert asyncio.run(scenario()) == 1
    assert closed == [1]
    assert rt.sessions.tenants() == {2}


def test_session_sweep_evicts_idle_and_survives_bad_close(tmp_path):
    rt = make_rt(tmp_path)

    class Rude:
        def close(self):
            raise RuntimeError("cannot close")

    registry = SessionRegistry(max_idle_s=0)
    registry.register_factory("google", lambda rt_, tid: Rude())

    async def scenario():
        await registry.get(rt, 1, "google")
        # max_idle_s=0 -> anything in the past is stale
        return await registry.sweep(now=registry._sessions[(1, "google")].last_used_at + 1)

    assert asyncio.run(scenario()) == 1     # a raising close() must not block eviction
    assert registry.live_count == 0


def test_session_capacity_evicts_least_recently_used(tmp_path):
    rt = make_rt(tmp_path)
    registry = SessionRegistry(max_sessions=2)
    registry.register_factory("google", lambda rt_, tid: {"t": tid})

    async def scenario():
        for tid in (1, 2, 3):
            await registry.get(rt, tid, "google")
        return registry.tenants()

    assert asyncio.run(scenario()) == {2, 3}   # tenant 1 was the oldest


def test_unknown_session_kind_is_a_clear_error(tmp_path):
    rt = make_rt(tmp_path)
    with pytest.raises(LookupError, match="no session factory"):
        asyncio.run(rt.sessions.get(rt, 1, "nope"))


# --- fail-closed production checks ------------------------------------------

def _settings(**kw) -> Settings:
    base = dict(telegram_bot_token="x", telegram_owner_id=1, _env_file=None)
    base.update(kw)
    return Settings(**base)


def test_single_user_mode_tolerates_dev_placeholders():
    check_production_secrets(_settings(multitenant_enabled=False))  # must not raise


def test_multitenant_refuses_to_boot_with_placeholder_secrets():
    with pytest.raises(InsecureConfigError) as exc:
        check_production_secrets(_settings(multitenant_enabled=True))
    assert "API_TOKEN_PEPPER" in str(exc.value)
    assert "JWT_SECRET" in str(exc.value)

    # fixing only one is still refused
    with pytest.raises(InsecureConfigError, match="JWT_SECRET"):
        check_production_secrets(
            _settings(multitenant_enabled=True, api_token_pepper="a" * 64)
        )
    # empty counts as unset
    with pytest.raises(InsecureConfigError, match="API_TOKEN_PEPPER"):
        check_production_secrets(
            _settings(multitenant_enabled=True, api_token_pepper="",
                      jwt_secret="b" * 64)
        )
    # both set -> boots
    check_production_secrets(
        _settings(multitenant_enabled=True, api_token_pepper="a" * 64,
                  jwt_secret="b" * 64)
    )


def test_audit_content_defaults_off_in_multitenant_mode():
    single = _settings(multitenant_enabled=False)
    assert single.store_audit_content is True          # unchanged for the owner

    multi = _settings(multitenant_enabled=True, api_token_pepper="a" * 64,
                      jwt_secret="b" * 64)
    assert multi.store_audit_content is False          # other people's messages

    # an operator can still opt in explicitly
    explicit = _settings(multitenant_enabled=True, api_token_pepper="a" * 64,
                         jwt_secret="b" * 64, audit_store_content=True)
    assert explicit.store_audit_content is True


# --- rate limiting -----------------------------------------------------------

def test_rate_limit_window_allows_then_blocks_then_recovers():
    from archon.api.ratelimit import SlidingWindow

    w = SlidingWindow(window_s=60)
    assert all(w.hit("ip", 3, now=100.0 + i) for i in range(3))
    assert w.hit("ip", 3, now=103.0) is False          # 4th in the window
    assert w.retry_after("ip", now=103.0) >= 1
    assert w.hit("ip", 3, now=200.0) is True           # window rolled past
    assert w.hit("other", 3, now=103.0) is True        # keys are independent
    assert w.hit("ip", 0, now=103.0) is True           # 0 disables the budget


def test_rate_limiter_is_off_for_the_single_user_tunnel(tmp_path):
    from archon.api.ratelimit import install
    from archon.api.server import build_app

    rt = make_rt(tmp_path)
    assert rt.settings.multitenant_enabled is False
    app = build_app(rt)
    assert getattr(app.state, "rate_limiter", None) is None

    class _App:
        state = type("S", (), {})()

        def middleware(self, _kind):
            raise AssertionError("middleware must not be installed in single-user mode")

    install(_App(), rt.settings)
