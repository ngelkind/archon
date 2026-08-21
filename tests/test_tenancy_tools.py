"""Tool-level tenancy: a tool dispatched under tenant B must read and write B's
rows, never tenant 1's.

``test_tenancy.py`` proves the repo layer isolates tenants. This file proves the
layer above it — ``ToolContext.store`` — actually carries the tenant into every
tool, which is what the ``ctx.rt.db`` -> ``ctx.store`` conversion bought. Each
test seeds two tenants with deliberately COLLIDING rows (same WhatsApp group,
same contact, same persona name, same setting key) so a scoping mistake shows up
as reading or clobbering the wrong tenant's data rather than as an empty result.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

from archon.api.ctx import tenant_ctx
from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, TenantScope, owner_scope, tenant_purge

from test_api import make_rt
from test_tenancy import _new_tenant, _seed


def _tool_rt(tmp_path):
    """Runtime with the owner-scoped toolset registered."""
    from archon.tools import (
        capture as capture_tools, contacts as contact_tools,
        contexts as context_tools, logging_ as logging_tools,
        scheduling as scheduling_tools, settings_ as settings_tools,
    )

    rt = make_rt(tmp_path)
    for mod in (settings_tools, context_tools, capture_tools, scheduling_tools,
                contact_tools, logging_tools):
        mod.register(rt.registry)
    return rt


def _call(rt, tenant_id, tool, args=None):
    return asyncio.run(
        rt.registry.dispatch(tenant_ctx(rt, tenant_id), tool, args or {})
    )


def _two_tenants(tmp_path):
    rt = _tool_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _seed(rt, OWNER_TENANT_ID, "A")
    _seed(rt, b_id, "B")
    return rt, b_id


# --- reads -------------------------------------------------------------------

def test_tool_reads_return_only_the_calling_tenants_rows(tmp_path):
    rt, b_id = _two_tenants(tmp_path)

    a_chats = json.loads(_call(rt, OWNER_TENANT_ID, "chat_list"))
    b_chats = json.loads(_call(rt, b_id, "chat_list"))
    # Each tenant sees its own seeded chat plus its OWN control/mind chat, which
    # building the context creates — and never the other tenant's.
    assert "Family-A" in [c["name"] for c in a_chats]
    assert "Family-B" not in [c["name"] for c in a_chats]
    assert "Family-B" in [c["name"] for c in b_chats]
    assert "Family-A" not in [c["name"] for c in b_chats]

    a_set = json.loads(_call(rt, OWNER_TENANT_ID, "settings_get",
                             {"key": "llm.active_provider"}))
    b_set = json.loads(_call(rt, b_id, "settings_get",
                             {"key": "llm.active_provider"}))
    assert a_set["llm.active_provider"] == "A"
    assert b_set["llm.active_provider"] == "B"


def test_tool_cannot_read_another_tenants_chat_by_exact_id(tmp_path):
    """B knows A's exact chat_id — the tool still resolves inside B's tenant."""
    rt = _tool_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    a = _seed(rt, OWNER_TENANT_ID, "A")          # only A has the chat

    out = json.loads(_call(rt, b_id, "context_show",
                           {"platform": "wa", "chat_id": "shared@g.us"}))
    assert out == {"error": "unknown chat"}
    # A's memory is untouched and still readable by A
    assert [r["content"] for r in repo.context_get(
        owner_scope(rt.db), a["chat_pk"], None)] == ["secret-A"]


# --- writes ------------------------------------------------------------------

def test_tool_writes_land_in_the_calling_tenant_only(tmp_path):
    rt, b_id = _two_tenants(tmp_path)

    out = json.loads(_call(rt, b_id, "whitelist_add",
                           {"platform": "wa", "chat_id": "shared@g.us"}))
    assert out["whitelisted"] is True
    assert repo.chat_get(TenantScope(rt.db, b_id), "wa",
                         "shared@g.us")["is_whitelisted"] == 1
    assert repo.chat_get(owner_scope(rt.db), "wa",
                         "shared@g.us")["is_whitelisted"] == 0


def test_settings_set_does_not_cross_tenants(tmp_path):
    rt, b_id = _two_tenants(tmp_path)
    _call(rt, b_id, "settings_set",
          {"key": "llm.active_provider", "value_json": '"openai"'})
    assert repo.setting_get(owner_scope(rt.db), "llm.active_provider") == "A"
    assert repo.setting_get(TenantScope(rt.db, b_id), "llm.active_provider") == "openai"


def test_personas_collide_by_name_without_interfering(tmp_path):
    rt, b_id = _two_tenants(tmp_path)
    out = json.loads(_call(rt, b_id, "persona_create",
                           {"name": "work", "system_prompt": "B rewritten"}))
    assert out["ok"] is True
    assert repo.persona_by_name(owner_scope(rt.db), "work")["system_prompt"] \
        == "prompt-A"
    assert repo.persona_by_name(TenantScope(rt.db, b_id), "work")["system_prompt"] \
        == "B rewritten"


def test_contact_tools_are_tenant_scoped(tmp_path):
    rt = _tool_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _seed(rt, OWNER_TENANT_ID, "A")              # A knows Dana

    found = json.loads(_call(rt, b_id, "contact_search", {"query": "Dana"}))
    assert found["matches"] == []                # B's directory is empty

    _call(rt, b_id, "contact_remember",
          {"name": "Dana", "phone": "+15550000000"})
    assert json.loads(_call(rt, b_id, "contacts_stats"))["entries"] == 1
    assert json.loads(_call(rt, OWNER_TENANT_ID, "contacts_stats"))["entries"] == 1
    # each tenant kept their own number for the same name
    assert repo.contact_list(owner_scope(rt.db))[0]["phone"] == "+972500000000"
    assert repo.contact_list(TenantScope(rt.db, b_id))[0]["phone"] == "+15550000000"


def test_schedule_and_capture_tools_are_scoped(tmp_path):
    rt, b_id = _two_tenants(tmp_path)

    a_sched = json.loads(_call(rt, OWNER_TENANT_ID, "schedule_list"))
    b_sched = json.loads(_call(rt, b_id, "schedule_list"))
    assert [s["text"] for s in a_sched] == ["sched-A"]
    assert [s["text"] for s in b_sched] == ["sched-B"]

    _call(rt, b_id, "capture_add", {"platform": "wa", "chat_id": "shared@g.us"})
    b_cap = json.loads(_call(rt, b_id, "capture_list"))
    a_cap = json.loads(_call(rt, OWNER_TENANT_ID, "capture_list"))
    assert len(b_cap["armed_chats"]) == 1
    assert a_cap["armed_chats"] == []            # A's identical chat not armed


# --- the conversion itself ---------------------------------------------------

def test_no_tool_reaches_the_database_unscoped():
    """Regression guard on the ctx.rt.db -> ctx.store conversion.

    The single permitted exception is the whole-file DB backup, which is a
    Db-level operation (copying the file) and has no per-tenant meaning.
    """
    offenders = []
    for path in sorted(pathlib.Path("src/archon/tools").rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "ctx.rt.db" in line:
                offenders.append(f"{path.name}:{i}")
    assert offenders == ["system.py:45"], offenders


# --- audit ------------------------------------------------------------------

def test_sensitive_tool_args_are_not_visible_to_another_tenant(tmp_path):
    """The leak this migration closed: `sensitive` tools audit their ARGUMENTS
    (recipients, names, numbers), which used to sit in one shared table."""
    rt, b_id = _two_tenants(tmp_path)

    _call(rt, b_id, "contact_remember",
          {"name": "B-secret-contact", "phone": "+15550001111"})
    _call(rt, OWNER_TENANT_ID, "contact_remember",
          {"name": "A-secret-contact", "phone": "+15550002222"})

    a_blob = str([dict(r) for r in repo.audit_query(owner_scope(rt.db), limit=100)])
    b_blob = str([dict(r) for r in repo.audit_query(TenantScope(rt.db, b_id),
                                                    limit=100)])
    assert "A-secret-contact" in a_blob and "A-secret-contact" not in b_blob
    assert "B-secret-contact" in b_blob and "B-secret-contact" not in a_blob


def test_system_audit_rows_stay_out_of_tenant_logs(tmp_path):
    """Process events belong to no tenant: the owner sees them (it is their
    box's operational history), a product tenant does not."""
    rt = _tool_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    rt.audit.note("subsystem_crash", subsystem="whatsapp")   # no tenant -> SYSTEM

    a_actions = [r["action"] for r in repo.audit_query(owner_scope(rt.db), limit=100)]
    b_actions = [r["action"] for r in repo.audit_query(TenantScope(rt.db, b_id),
                                                       limit=100)]
    assert "subsystem_crash" in a_actions
    assert "subsystem_crash" not in b_actions


def test_audit_query_tool_is_scoped(tmp_path):
    rt = _tool_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _call(rt, OWNER_TENANT_ID, "contact_remember",
          {"name": "OwnerOnlyName", "phone": "+15550003333"})

    assert "OwnerOnlyName" not in _call(rt, b_id, "audit_query")
    assert "OwnerOnlyName" in _call(rt, OWNER_TENANT_ID, "audit_query")


def test_purging_a_tenant_removes_their_audit_rows(tmp_path):
    rt = _tool_rt(tmp_path)
    b_id = _new_tenant(rt, "b@example.com")
    _call(rt, b_id, "contact_remember", {"name": "B-gone", "phone": "+15550004444"})
    assert repo.audit_query(TenantScope(rt.db, b_id), limit=100)

    tenant_purge(rt.db, b_id)
    assert repo.audit_query(TenantScope(rt.db, b_id), limit=100) == []
