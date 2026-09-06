"""Dispatch every tool for real and watch what reaches the database layer.

WHY THIS FILE EXISTS. `7214205` fixed six sites in ``tools/calendar.py`` that
passed ``ctx.rt`` (the Runtime) to ``_default_calendar(store)``, which forwards
it to ``repo.setting_get``. Live, every Google calendar tool returned
``{"error": "AttributeError: 'Runtime' object has no attribute 'query_one'"}``.
The suite was fully green throughout, for three separate reasons — each of which
this file closes:

1. ``test_tenancy_tools.py:_tool_rt`` registers six tool modules and does not
   include ``calendar`` or ``email_``. Those tools were never dispatched by any
   test, so no assertion could have failed.
2. ``test_no_tool_reaches_the_database_unscoped`` greps for the literal string
   ``ctx.rt.db``. The bug was ``ctx.rt`` — a *different spelling of the same
   mistake*, which that grep cannot match by construction. A textual guard only
   catches the wording it was written against; this file checks BEHAVIOUR, so it
   catches any spelling, including ones nobody has thought of yet.
3. ``Registry.dispatch`` catches every exception and returns it as a JSON error
   string (``registry.py``, the ``except Exception`` at the end). Nothing
   propagates. So a test that dispatches a tool and merely checks it did not
   raise is VACUOUSLY GREEN while the tool is completely broken. Every
   assertion here therefore inspects either the recorded scope or the returned
   payload — never merely "it didn't blow up".

The mechanism is a ledger: every public ``repo.*`` function is wrapped to record
the store it was handed. Recording rather than raising is deliberate — an
exception would be swallowed by dispatch and lost, whereas the ledger survives
and can be asserted on afterwards.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from archon.api.ctx import tenant_ctx
from archon.db import repo
from archon.db.tenancy import OWNER_TENANT_ID, Db, TenantScope

from test_api import make_rt
from test_tenancy import _new_tenant, _seed

# Modules whose tools reach a remote service we stub, or none at all. Everything
# registered gets dispatched; see ALL_TOOL_MODULES.
ALL_TOOL_MODULES = (
    "calendar", "email_", "capture", "contacts", "contexts", "llm_admin",
    "logging_", "media", "scheduling", "settings_", "subbots", "system",
    "telegram", "websearch", "whatsapp",
)

#: Tools that make a real outbound network call we do not stub. Dispatching
#: these would make the suite slow and internet-dependent for no extra
#: guarantee, so they are excluded from the sweep — and
#: ``test_excluded_tools_never_touch_the_database`` proves the exclusion is safe
#: by checking each one's own source holds no ``repo.`` call.
#:
#: ``web_search`` is deliberately NOT here even though it lives beside
#: ``web_fetch``: it reads ``web.ddg_enabled`` through ``ctx.store``, so it
#: carries a scope and must be watched. It stays offline because that setting
#: defaults to False and the network branch is skipped. The first draft of this
#: file excluded it by module and the justification test below caught that —
#: which is why the check is per-handler, not per-file.
NETWORK_TOOLS = frozenset({"web_fetch", "download_video", "attach_image_from_url"})


# --- the ledger --------------------------------------------------------------
#
# The recorder itself lives in archon.testing.repo_ledger so the end-to-end
# harness and the live probe runner share it; this fixture only binds it to the
# test's monkeypatch lifetime.

from archon.testing.repo_ledger import Ledger as _Ledger, unwrap_repo, wrap_repo  # noqa: E402


@pytest.fixture
def ledger() -> _Ledger:
    """Wrap every public repo function so its store argument is recorded.

    Wrapping the module wholesale rather than naming functions individually is
    the point: a repo function added tomorrow is covered automatically, with no
    chance of the list going stale the way a hand-maintained one would.
    """
    led = _Ledger()
    originals = wrap_repo(led)
    try:
        yield led
    finally:
        unwrap_repo(originals)


# --- fixtures ----------------------------------------------------------------

class _FakeGoogle:
    """Stands in for the Gmail/Calendar client.

    Every method returns an empty JSON-safe structure so the tool body runs all
    the way through to its ``json.dumps`` instead of dying early — a client that
    raised would stop execution before the scoping mistake could be reached.
    """

    def __getattr__(self, _name):
        def call(*_args, **_kwargs):
            return {}
        return call


@pytest.fixture
def rt_with_tools(tmp_path, monkeypatch):
    """Runtime with EVERY tool module registered and Google stubbed out."""
    import importlib

    from archon.integrations import google as google_integration

    rt = make_rt(tmp_path)
    for mod_name in ALL_TOOL_MODULES:
        mod = importlib.import_module(f"archon.tools.{mod_name}")
        # system_tools is already registered by make_rt.
        if mod_name == "system":
            continue
        mod.register(rt.registry)

    async def _fake_client_for(_rt, _tenant_id, _which):
        return _FakeGoogle()

    monkeypatch.setattr(google_integration, "client_for", _fake_client_for)
    return rt


#: Plausible arguments for every tool that needs them. These exist so the
#: handler BODY actually executes: dispatch binds args by signature, and a
#: missing required argument raises TypeError before the body runs — which would
#: make every assertion here vacuous. `test_no_tool_was_rejected_before_its_body_ran`
#: enforces that this table stays complete.
TOOL_ARGS: dict[str, dict] = {
    "chat_log_policy_set": {"platform": "wa", "chat_id": "shared@g.us", "enabled": True},
    "contact_remember": {"name": "Dana", "phone": "+15550001111"},
    "contact_search": {"query": "Dana"},
    "contacts_import": {"path": "nope.vcf"},
    "context_clear": {"platform": "wa", "chat_id": "shared@g.us"},
    "context_show": {"platform": "wa", "chat_id": "shared@g.us"},
    "cost_breakdown": {"period": "day"},
    "delay_policy_set": {"platform": "wa", "chat_id": "shared@g.us", "mode": "off"},
    "download_video": {"url": "https://example.invalid/v"},
    "email_mark_read": {"gmail_msg_id": "m1"},
    "email_read": {"gmail_msg_id": "m1"},
    "email_reply": {"gmail_msg_id": "m1", "body": "hi"},
    "email_search": {"query": "invoice"},
    "email_send": {"to": "a@example.com", "subject": "s", "body": "b"},
    "image_recognition_set": {"platform": "wa", "chat_id": "shared@g.us", "enabled": True},
    "llm_set_key": {"provider": "gemini", "api_key": "k"},
    "llm_set_model": {"provider": "gemini", "tier": "cheap", "model": "m"},
    "llm_set_provider": {"provider": "gemini"},
    "log_channel_set": {"channel_id": -100123},
    "pending_reply_cancel": {"reply_id": 1},
    "persona_assign": {"platform": "wa", "chat_id": "shared@g.us", "persona_name": "work"},
    "persona_create": {"name": "new", "system_prompt": "p"},
    "persona_delete": {"name": "work"},
    "redaction_set": {"enabled": True},
    "schedule_cancel": {"schedule_id": 1},
    "schedule_message": {"platform": "wa", "chat_id": "shared@g.us",
                         "text": "t", "due_iso": "2099-01-01T00:00:00"},
    "send_policy_set": {"platform": "wa", "chat_id": "shared@g.us", "policy": "auto"},
    "settings_set": {"key": "k", "value_json": '"v"'},
    "subbot_register": {"token": "t", "platform_scope": "tg"},
    "subbot_remove": {"subbot_id": 1},
    "subbot_set_enabled": {"subbot_id": 1, "enabled": True},
    "tg_get_history": {"chat_id": "123"},
    "tg_notify_owner": {"text": "hi"},
    "tg_send_group": {"chat_id": "123", "text": "hi"},
    "tg_send_private": {"chat_id": "123", "text": "hi"},
    "wa_check_number": {"phone": "+15550001111"},
    "wa_get_history": {"chat_jid": "shared@g.us"},
    "wa_mark_read": {"chat_jid": "shared@g.us"},
    "wa_send_image": {"chat_jid": "shared@g.us", "image_path": "x.png"},
    "wa_send_message": {"chat_jid": "shared@g.us", "text": "hi"},
    "web_fetch": {"url": "https://example.invalid"},
    "web_search": {"query": "q"},
    "whitelist_add": {"platform": "wa", "chat_id": "shared@g.us"},
    "whitelist_remove": {"platform": "wa", "chat_id": "shared@g.us"},
    "calendar_create_event": {"title": "t", "start_iso": "2099-01-01T10:00:00"},
    "calendar_update_event": {"event_id": "e1", "title": "t2"},
    "calendar_delete_event": {"event_id": "e1"},
    "calendar_search_events": {"query": "dentist"},
    "calendar_list_events": {"period": "today"},
    "calendar_free_busy": {"start_iso": "2099-01-01T10:00:00",
                           "end_iso": "2099-01-01T11:00:00"},
    "calendar_check_conflicts": {"start_iso": "2099-01-01T10:00:00",
                                 "end_iso": "2099-01-01T11:00:00"},
    "calendar_set_default": {"calendar_id": "primary"},
    "attach_image_from_url": {"url": "https://example.invalid/i.png"},
    "auto_reply_set": {"platform": "wa", "chat_id": "shared@g.us", "enabled": True},
    "budget_set": {"usd_per_day": 1.0},
    "capture_add": {"platform": "wa", "chat_id": "shared@g.us"},
    "capture_remove": {"platform": "wa", "chat_id": "shared@g.us"},
    "capture_all_dms_set": {"enabled": True},
    "chat_find": {"approx_name": "Family"},
}


def _tool_names(rt, prefix: str = "") -> list[str]:
    return sorted(
        n for n in rt.registry._tools
        if n.startswith(prefix) and n not in NETWORK_TOOLS
    )


def _dispatch(rt, tenant_id: int, name: str) -> str:
    return asyncio.run(
        rt.registry.dispatch(tenant_ctx(rt, tenant_id), name, TOOL_ARGS.get(name, {}))
    )


# --- the guard the lead asked for: Google-backed tools -----------------------

GOOGLE_PREFIXES = ("calendar_", "email_")


def _google_tools(rt) -> list[str]:
    return [n for n in _tool_names(rt)
            if any(n.startswith(p) for p in GOOGLE_PREFIXES)]


def test_google_tools_are_actually_registered(rt_with_tools):
    """Non-vacuity guard for this whole file.

    If the calendar/email modules stopped registering, every parametrised test
    below would silently collapse to zero cases and stay green. `7214205`
    happened underneath exactly that kind of hole.
    """
    names = _google_tools(rt_with_tools)
    assert len(names) >= 8, names
    assert any(n.startswith("calendar_") for n in names)
    assert any(n.startswith("email_") for n in names)


def test_every_google_tool_hands_repo_a_real_scope(rt_with_tools, ledger):
    """The exact regression. Before `7214205` this fails on six calendar tools.

    Asserted per tool rather than in aggregate so a failure names the offender.
    """
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    broken: dict[str, list[str]] = {}
    for name in _google_tools(rt_with_tools):
        before = len(ledger.calls)
        _dispatch(rt_with_tools, b_id, name)
        offenders = [
            f"repo.{fn}(<{type(store).__name__}>)"
            for fn, store in ledger.calls[before:]
            if not isinstance(store, (Db, TenantScope))
        ]
        if offenders:
            broken[name] = offenders
    assert broken == {}, broken


def test_every_google_tool_uses_the_calling_tenants_scope(rt_with_tools, ledger):
    """Not merely 'a scope' — tenant B's scope.

    A Runtime is not the only way to get this wrong; hardcoding the owner is the
    other, and it is the one that leaks rather than errors.
    """
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    wrong: dict[str, set[int]] = {}
    for name in _google_tools(rt_with_tools):
        before = len(ledger.calls)
        _dispatch(rt_with_tools, b_id, name)
        seen = {s.tenant_id for _, s in ledger.calls[before:]
                if isinstance(s, TenantScope)}
        if seen - {b_id}:
            wrong[name] = seen
    assert wrong == {}, wrong
    assert OWNER_TENANT_ID != b_id       # the assertion above means something


def test_calendar_tools_reach_the_database_at_all(rt_with_tools, ledger):
    """Proves the previous two tests are not passing on an empty ledger.

    Every calendar tool resolves the default calendar id through
    ``repo.setting_get`` — that call is precisely where the bug lived, so if it
    is absent the guard is watching nothing.
    """
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    silent = []
    for name in _google_tools(rt_with_tools):
        if not name.startswith("calendar_"):
            continue
        before = len(ledger.calls)
        _dispatch(rt_with_tools, b_id, name)
        if not ledger.calls[before:]:
            silent.append(name)
    assert silent == [], silent


def test_no_google_tool_returns_a_programming_error(rt_with_tools):
    """The live symptom itself, asserted on the returned payload.

    Dispatch converts exceptions into ``{"error": "AttributeError: ..."}``, so
    this reads the JSON rather than expecting a raise. AttributeError/TypeError
    from inside a tool are always bugs; a tool legitimately reporting e.g. a
    missing credential is not.
    """
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    bad = {}
    for name in _google_tools(rt_with_tools):
        out = _dispatch(rt_with_tools, b_id, name)
        try:
            err = json.loads(out).get("error", "")
        except (json.JSONDecodeError, AttributeError):
            continue
        if err.startswith(("AttributeError", "TypeError")):
            bad[name] = err
    assert bad == {}, bad


# --- the general guard: every tool, not just Google --------------------------

def test_no_tool_anywhere_hands_repo_something_that_is_not_a_scope(
    rt_with_tools, ledger
):
    """The whole bug class, across every registered tool.

    This is the one that catches the NEXT missed site, wherever it is. The
    calendar miss was found in production because no test dispatched the tool;
    this dispatches all of them, so a module added later is covered the moment
    it registers.
    """
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    broken: dict[str, list[str]] = {}
    for name in _tool_names(rt_with_tools):
        before = len(ledger.calls)
        _dispatch(rt_with_tools, b_id, name)
        offenders = [
            f"repo.{fn}(<{type(store).__name__}>)"
            for fn, store in ledger.calls[before:]
            if not isinstance(store, (Db, TenantScope))
        ]
        if offenders:
            broken[name] = sorted(set(offenders))
    assert broken == {}, broken


def test_no_tool_leaks_into_another_tenants_scope(rt_with_tools, ledger):
    """Every scope reaching repo during tenant B's dispatch belongs to B."""
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    wrong: dict[str, set[int]] = {}
    for name in _tool_names(rt_with_tools):
        before = len(ledger.calls)
        _dispatch(rt_with_tools, b_id, name)
        seen = {s.tenant_id for _, s in ledger.calls[before:]
                if isinstance(s, TenantScope)}
        if seen - {b_id}:
            wrong[name] = seen
    assert wrong == {}, wrong


def test_no_tool_was_rejected_before_its_body_ran(rt_with_tools):
    """Keeps TOOL_ARGS honest.

    ``dispatch`` binds arguments by signature, so a tool missing a required one
    raises TypeError before a single line of its body executes — and every
    scoping assertion above would pass while proving nothing. This fails when a
    new required argument appears, forcing TOOL_ARGS to be updated instead of
    the coverage silently rotting away.
    """
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    unbound = {}
    for name in _tool_names(rt_with_tools):
        out = _dispatch(rt_with_tools, b_id, name)
        try:
            err = json.loads(out).get("error", "")
        except (json.JSONDecodeError, AttributeError):
            continue
        if "required positional argument" in err or "required keyword" in err:
            unbound[name] = err
    assert unbound == {}, unbound


def test_the_sweep_actually_observes_database_traffic(rt_with_tools, ledger):
    """Anti-vacuity floor for this whole file.

    Every assertion here is of the form "nothing bad reached repo". That is
    trivially true if NOTHING reached repo — a stubbed-out fixture, a fixture
    that stopped registering tools, or a ledger that quietly detached would all
    leave the file green while testing nothing. At the time of writing the sweep
    dispatches 75 tools and observes ~177 repo calls, every tool reaching the
    database at least once. The floor is set well below that so ordinary churn
    does not trip it, while a collapse to zero does.
    """
    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    swept = _tool_names(rt_with_tools)
    for name in swept:
        _dispatch(rt_with_tools, b_id, name)

    assert len(swept) >= 60, f"only {len(swept)} tools swept — did registration break?"
    assert len(ledger.calls) >= 100, (
        f"only {len(ledger.calls)} repo calls observed across {len(swept)} tools; "
        "the guard is no longer watching real traffic"
    )
    assert ledger.tenants() == {b_id}


def test_excluded_tools_never_touch_the_database(rt_with_tools):
    """Justifies NETWORK_TOOLS being skipped rather than silently dropped.

    They are excluded to keep the suite offline and fast. That is only safe
    while they hold no scope at all — if one grows a repo call it must rejoin
    the sweep, and this fails until it does.

    Checked against each HANDLER's own source rather than its module: the first
    draft checked the file, which lumped ``web_fetch`` together with
    ``web_search`` in the same module and wrongly excluded a tool that does
    carry a scope.
    """
    for tool in sorted(NETWORK_TOOLS):
        handler = rt_with_tools.registry.get(tool).handler
        src = inspect.getsource(handler)
        assert "repo." not in src, (
            f"{tool} now uses repo — remove it from NETWORK_TOOLS so the sweep "
            "covers it, and stub whatever it calls out to"
        )


# --- mutation guard ----------------------------------------------------------

def test_this_file_would_have_caught_the_original_bug(rt_with_tools, ledger,
                                                      monkeypatch):
    """Re-introduce `7214205` verbatim and prove the guard fires.

    A regression test nobody has seen fail is a guess. This puts ``ctx.rt`` back
    where it was and asserts the ledger reports it, so the guard is known to
    work rather than merely believed to.
    """
    from archon.tools import calendar as calendar_tools

    b_id = _new_tenant(rt_with_tools, "b@example.com")
    _seed(rt_with_tools, b_id, "B")

    # A fresh registry carrying the pre-fix calendar tools: the handler asks for
    # the default calendar with the Runtime, exactly as it did before the fix.
    real_default = calendar_tools._default_calendar
    monkeypatch.setattr(
        calendar_tools, "_default_calendar",
        lambda store: real_default(store),
    )

    from archon.tools.registry import Registry, ToolContext
    broken_registry = Registry()

    @broken_registry.tool("calendar_list_calendars_broken", "pre-7214205 shape")
    async def _broken(ctx: ToolContext) -> str:
        # THE BUG: ctx.rt is the Runtime; _default_calendar expects a store.
        return json.dumps({"default": calendar_tools._default_calendar(ctx.rt)})

    ctx = tenant_ctx(rt_with_tools, b_id)
    ctx.rt.registry = broken_registry
    before = len(ledger.calls)
    out = asyncio.run(
        broken_registry.dispatch(ctx, "calendar_list_calendars_broken", {})
    )

    offenders = [
        f"repo.{fn}(<{type(store).__name__}>)"
        for fn, store in ledger.calls[before:]
        if not isinstance(store, (Db, TenantScope))
    ]
    assert offenders == ["repo.setting_get(<Runtime>)"], offenders
    # ...and the live symptom, swallowed into JSON exactly as it was on the box.
    assert "AttributeError" in json.loads(out)["error"]


def test_dispatch_swallows_exceptions_so_the_ledger_is_necessary():
    """Documents the reason this file asserts on a ledger, not on a raise.

    If dispatch ever stops swallowing, this fails and the elaborate machinery
    above can be simplified.
    """
    from archon.tools.registry import Registry, ToolContext
    reg = Registry()

    @reg.tool("boom", "always raises")
    async def _boom(ctx: ToolContext) -> str:
        raise AttributeError("'Runtime' object has no attribute 'query_one'")

    import pathlib as _p
    rt = make_rt(_p.Path(__import__("tempfile").mkdtemp()))
    out = asyncio.run(reg.dispatch(ToolContext(rt=rt, scope="owner"), "boom", {}))
    assert json.loads(out)["error"].startswith("AttributeError")
