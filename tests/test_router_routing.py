"""Per-purpose, context-aware LLM routing (the owner's provider policy).

Rules under test:
- a private-chat reply ("dm") uses anthropic then gemini, NEVER openrouter/nvidia;
- tool/agent calls use gemini/anthropic/openrouter (a tool-capable provider);
- vision uses a vision-capable provider (gemini here);
- everything else (triage/classification/group reply) uses nvidia then gemini;
- a provider out of balance warns the owner and the router falls through;
- an exhausted chain raises; a forced provider pins every route.
"""

from __future__ import annotations

import asyncio

import pytest
from test_m2 import make_rt

from archon.db import repo
from archon.llm.base import ChatMessage, ImagePart, LLMResult, ProviderError, Usage
from archon.llm.router import Router


class Fake:
    supports_tools = True
    supports_vision = True

    def __init__(self, name: str, behavior: str = "ok") -> None:
        self.name = name
        self.behavior = behavior
        self.calls = 0

    async def complete(self, **kw):
        self.calls += 1
        if self.behavior == "no_balance":
            raise ProviderError("Error code: 402 - insufficient credit balance")
        if self.behavior == "error":
            raise ProviderError("boom")
        return LLMResult(text="ok", tool_calls=[], usage=Usage(1, 1),
                         model=kw["model"], provider=self.name, stop_reason="end")


def _router(rt, **providers) -> Router:
    r = Router(rt)
    rt.router = r
    for name, beh in providers.items():
        r._providers[name] = Fake(name, beh)
    return r


def _run(coro):
    return asyncio.run(coro)


def test_dm_reply_uses_anthropic_then_gemini(tmp_path):
    rt = make_rt(tmp_path)
    r = _router(rt, anthropic="ok", gemini="ok", openrouter="ok", nvidia="ok")
    res = _run(r.complete(purpose="reply", context="dm", system="s",
                          messages=[ChatMessage(role="user", text="hi")]))
    assert res.provider == "anthropic"
    assert r._providers["openrouter"].calls == 0 and r._providers["nvidia"].calls == 0
    # anthropic down -> gemini, still never openrouter/nvidia
    r._providers["anthropic"].behavior = "error"
    res2 = _run(r.complete(purpose="reply", context="dm", system="s",
                           messages=[ChatMessage(role="user", text="hi")]))
    assert res2.provider == "gemini"
    assert r._providers["openrouter"].calls == 0


def test_dm_never_falls_to_openrouter_even_as_last_resort(tmp_path):
    rt = make_rt(tmp_path)  # only gemini has a key; anthropic absent
    r = _router(rt, openrouter="ok")  # openrouter available, but NOT in the dm chain
    with pytest.raises(ProviderError, match="no provider could handle"):
        _run(r.complete(purpose="reply", context="dm", system="s",
                        messages=[ChatMessage(role="user", text="hi")]))
    assert r._providers["openrouter"].calls == 0


def test_tool_route_uses_a_tool_capable_provider(tmp_path):
    rt = make_rt(tmp_path)
    r = _router(rt, gemini="ok", anthropic="ok", openrouter="ok", nvidia="ok")
    res = _run(r.complete(purpose="agent", system="s",
                          messages=[ChatMessage(role="user", text="do it")],
                          tools=[]))  # bool(tools)==False; use purpose to select
    assert res.provider == "gemini"  # first in the tool chain


def test_vision_route_uses_a_vision_provider(tmp_path):
    rt = make_rt(tmp_path)
    r = _router(rt, gemini="ok", nvidia="ok")
    img = ChatMessage(role="user", text="what is this",
                      images=[ImagePart(data=b"x", mime="image/png")])
    res = _run(r.complete(purpose="vision", system="s", messages=[img]))
    assert res.provider == "gemini"


def test_default_route_uses_nvidia_first(tmp_path):
    rt = make_rt(tmp_path)
    r = _router(rt, nvidia="ok", gemini="ok")
    res = _run(r.complete(purpose="triage", system="s",
                          messages=[ChatMessage(role="user", text="classify")]))
    assert res.provider == "nvidia"


def test_no_balance_warns_owner_and_falls_through(tmp_path):
    rt = make_rt(tmp_path)
    r = _router(rt, nvidia="no_balance", gemini="ok")
    res = _run(r.complete(purpose="triage", system="s",
                          messages=[ChatMessage(role="user", text="hi")]))
    assert res.provider == "gemini"                    # fell through
    assert rt.health.get("llm:nvidia") == "out of balance"  # and warned


def test_exhausted_chain_raises(tmp_path):
    rt = make_rt(tmp_path)
    r = _router(rt, nvidia="error", gemini="error")
    with pytest.raises(ProviderError, match="no provider could handle"):
        _run(r.complete(purpose="triage", system="s",
                        messages=[ChatMessage(role="user", text="hi")]))


def test_force_provider_pins_every_route(tmp_path):
    rt = make_rt(tmp_path)
    repo.setting_set(rt.db, "llm.force_provider", "gemini")
    r = _router(rt, gemini="ok", nvidia="ok", anthropic="ok")
    # even a dm reply goes to the forced provider
    res = _run(r.complete(purpose="reply", context="dm", system="s",
                          messages=[ChatMessage(role="user", text="hi")]))
    assert res.provider == "gemini" and r._providers["nvidia"].calls == 0
