"""The NVIDIA build.nvidia.com provider wiring.

NVIDIA speaks OpenAI's chat dialect, so the provider reuses the OpenAI machinery
against a different base URL. These check the router picks it up, the free-tier
cost is $0, and nemotron's <think> reasoning is stripped so the triage JSON
parser sees clean text. (Live behaviour is proven separately against the real
endpoint; the tool-less path returns clean JSON.)
"""

from __future__ import annotations

import asyncio

from test_m2 import make_rt

from archon.llm.base import ChatMessage, LLMResult, Usage
from archon.llm.cost import compute_cost
from archon.llm.nvidia import NVIDIA_BASE_URL, NvidiaProvider
from archon.llm.router import PROVIDER_DEFAULTS, Router


def test_router_resolves_nvidia_when_keyed(tmp_path):
    rt = make_rt(tmp_path)
    rt.settings.nvidia_api_key = "nvapi-test"
    router = Router(rt)
    provider = router._get_provider("nvidia")
    assert isinstance(provider, NvidiaProvider)
    assert provider.name == "nvidia"
    assert provider.supports_tools and provider.supports_vision
    assert str(provider._client.base_url).rstrip("/") == NVIDIA_BASE_URL


def test_nvidia_defaults_and_free_cost():
    assert "nvidia" in PROVIDER_DEFAULTS
    assert "nemotron" in PROVIDER_DEFAULTS["nvidia"]["strong"]
    # build.nvidia.com free tier: recorded at $0, never a spurious price-unknown.
    u = Usage(in_tokens=1_000_000, out_tokens=1_000_000)
    assert compute_cost(None, "nvidia", "nvidia/nemotron-3-super-120b-a12b", u) == 0.0


def test_nemotron_reasoning_block_is_stripped(tmp_path):
    rt = make_rt(tmp_path)
    rt.settings.nvidia_api_key = "nvapi-test"
    provider = Router(rt)._get_provider("nvidia")

    async def fake_super(**kw):
        return LLMResult(
            text='<think>Let me reason about this at length...</think>\n{"action":"event"}',
            tool_calls=[], usage=Usage(10, 20), model=kw["model"],
            provider="nvidia", stop_reason="stop")

    # Replace the inherited OpenAI complete with our fake, so we test only the
    # nemotron post-processing NvidiaProvider adds.
    import archon.llm.openai_api as oai
    orig = oai.OpenAIProvider.complete
    oai.OpenAIProvider.complete = lambda self, **kw: fake_super(**kw)
    try:
        result = asyncio.run(provider.complete(
            model="nvidia/nemotron-3-super-120b-a12b", system="classify",
            messages=[ChatMessage(role="user", text="hi")], max_tokens=100))
    finally:
        oai.OpenAIProvider.complete = orig
    assert "<think>" not in result.text
    assert result.text == '{"action":"event"}'
