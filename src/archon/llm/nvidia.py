"""NVIDIA build.nvidia.com backend — the OpenAI-compatible NIM endpoint.

NVIDIA's endpoint speaks the same chat.completions dialect as OpenAI, so this
reuses the whole OpenAI provider and only swaps the base URL and a little
nemotron-specific handling:

* Nemotron models are REASONING models — they emit a ``<think>…</think>`` block
  before the answer. ``detailed thinking off`` in the system prompt suppresses
  most of it; we strip any residual block so the triage JSON parser sees clean
  text.

TOOL-CALLING CAVEAT (measured 2026-09-09 against this account): the nemotron
models this endpoint actually serves do NOT reliably emit OpenAI-shaped
``tool_calls`` — they narrate the call in prose instead. So the tool-loop agent
(calendar-from-message, the owner /ask agent) is BEST-EFFORT on NVIDIA, while
the tool-less purposes (triage, reply, and vision on a *-vision-instruct model)
are solid. Pick the active provider per purpose accordingly, or keep a
tool-capable provider for the agent. Left ``supports_tools = True`` so the router
still offers tools; the model does what it can.
"""

from __future__ import annotations

import re
from typing import Any

import openai

from .openai_api import OpenAIProvider

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_NO_THINK = "detailed thinking off"


class NvidiaProvider(OpenAIProvider):
    name = "nvidia"
    supports_tools = True   # the API accepts tools; nemotron support is best-effort
    supports_vision = True  # via meta/llama-3.2-*-vision-instruct on the cheap tier

    def __init__(self, api_key: str, *, rt: Any = None) -> None:
        kw: dict[str, Any] = {"api_key": api_key, "base_url": NVIDIA_BASE_URL}
        if rt is not None:
            from ..net.client import new_async_client
            kw["http_client"] = new_async_client(rt, subsystem="llm", purpose="nvidia")
        self._client = openai.AsyncOpenAI(**kw)

    async def complete(self, *, model, system, messages, tools=None, max_tokens=4096,
                       json_only=False, native_web_search=False):
        # Keep nemotron terse so the classifier parses cleanly; harmless elsewhere.
        sys_prompt = f"{_NO_THINK}\n{system}" if "nemotron" in model.lower() else system
        result = await super().complete(
            model=model, system=sys_prompt, messages=messages, tools=tools,
            max_tokens=max_tokens, json_only=json_only,
            native_web_search=native_web_search)
        if result.text and "<think>" in result.text.lower():
            result.text = _THINK.sub("", result.text).strip()
        return result
