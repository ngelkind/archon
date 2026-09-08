"""Cost table honesty and the vision-provider guard.

Covers the fixes in Workstream F:
- Claude Sonnet 5 is priced at its real $2/$10 (was booked as $3/$15).
- Claude Fable 5 / 5.1 have entries instead of falling through to $0.
- an unknown model is AUDITED (llm_price_unknown) once, not silently $0.
- the router refuses to send images to a provider that cannot see them,
  instead of letting the picture be dropped and the model hallucinate.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_m2 import FakeProvider, make_rt

from archon.llm.base import ChatMessage, ImagePart, LLMResult, ProviderError, Usage
from archon.llm.cost import compute_cost
from archon.llm.router import Router

_MILLION = Usage(in_tokens=1_000_000, out_tokens=1_000_000)


def test_sonnet5_and_fable_prices(tmp_path):
    rt = make_rt(tmp_path)
    # Sonnet 5 is $2 in / $10 out — 1M+1M tokens = $12, not the old $18.
    assert compute_cost(rt.db, "anthropic", "claude-sonnet-5", _MILLION) == pytest.approx(12.0)
    # Fable 5.1 and Fable 5 are $10/$50; longest-prefix keeps them distinct.
    assert compute_cost(rt.db, "anthropic", "claude-fable-5-1", _MILLION) == pytest.approx(60.0)
    assert compute_cost(rt.db, "anthropic", "claude-fable-5", _MILLION) == pytest.approx(60.0)


def test_unknown_priced_model_is_audited_once(tmp_path):
    rt = make_rt(tmp_path)
    # gemini has a price table, so an unrecognised gemini model is a stale-table
    # gap worth surfacing — not the silent $0 it used to be.
    cost = compute_cost(rt.db, "gemini", "gemini-99-imaginary", _MILLION, rt=rt)
    assert cost == 0.0
    # calling again must NOT add a second warning (warn-once per provider/model)
    compute_cost(rt.db, "gemini", "gemini-99-imaginary", _MILLION, rt=rt)

    entries = [json.loads(line) for line in
               rt.settings.archon_data.joinpath("a.jsonl").read_text().splitlines()
               if line.strip()]
    warns = [e for e in entries if e.get("action") == "llm_price_unknown"]
    assert len(warns) == 1, f"expected exactly one warning, got {len(warns)}"
    assert warns[0]["model"] == "gemini-99-imaginary"
    assert "unknown model" in rt.health.get("llm_pricing", "")


def test_unknown_model_without_rt_stays_silent_and_zero(tmp_path):
    # Back-compat: the db-only signature (no rt) must not crash and returns $0.
    rt = make_rt(tmp_path)
    assert compute_cost(rt.db, "gemini", "still-unknown", _MILLION) == 0.0
    assert "llm_pricing" not in rt.health


def test_openrouter_empty_table_is_not_a_spurious_warning(tmp_path):
    # openrouter prices out-of-band (empty table by design): a missing model
    # there is expected, so it must NOT raise a stale-table warning.
    rt = make_rt(tmp_path)
    assert compute_cost(rt.db, "openrouter", "some/model", _MILLION, rt=rt) == 0.0
    assert "llm_pricing" not in rt.health


def _img_message() -> ChatMessage:
    return ChatMessage(role="user", text="what is this?",
                       images=[ImagePart(data=b"\x89PNG", mime="image/png")])


def test_router_refuses_images_to_a_blind_provider(tmp_path):
    rt = make_rt(tmp_path)  # active provider is "gemini"
    router = Router(rt)
    rt.router = router

    class BlindProvider(FakeProvider):
        name = "gemini"
        supports_vision = False

    router._providers["gemini"] = BlindProvider()
    with pytest.raises(ProviderError, match="cannot see images"):
        asyncio.run(router.complete(purpose="vision", system="s",
                                    messages=[_img_message()]))


def test_router_allows_images_to_a_sighted_provider(tmp_path):
    rt = make_rt(tmp_path)
    router = Router(rt)
    rt.router = router

    class SightedProvider(FakeProvider):
        name = "gemini"
        supports_vision = True

        async def complete(self, **kw):
            return LLMResult(text="a cat", tool_calls=[], usage=Usage(10, 5),
                             model=kw["model"], provider="gemini", stop_reason="end")

    router._providers["gemini"] = SightedProvider()
    result = asyncio.run(router.complete(purpose="vision", system="s",
                                         messages=[_img_message()]))
    assert result.text == "a cat"
