"""A deterministic LLM provider driven by a per-scenario script.

The router resolves a model per purpose tier (cheap for triage/vision/reply,
strong for the agent); the harness installs this provider as ``scripted`` with
``llm.model.scripted.cheap = "scripted:cheap"`` and ``…strong = "scripted:strong"``,
so a step can target a tier without the provider having to guess from prompts.

A call that matches no step is recorded in :attr:`unscripted` and raises
:class:`UnscriptedLLMCall` — deliberately NOT a ``ProviderError``, because the
pipeline converts those into a silent ``ignore`` verdict (the very failure mode
the suite exists to catch). The harness asserts ``unscripted == []`` on stop,
so a scenario can never pass on an answer nobody scripted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..llm.base import ChatMessage, LLMResult, ProviderError, ToolCall, ToolSpec, Usage

PROVIDER_NAME = "scripted"
CHEAP_MODEL = "scripted:cheap"
STRONG_MODEL = "scripted:strong"


class UnscriptedLLMCall(RuntimeError):
    """The scenario did not say what the model should answer here."""


@dataclass(slots=True)
class Request:
    """One call the router made, as the provider saw it."""

    model: str
    system: str
    messages: list[ChatMessage]
    tools: list[ToolSpec]
    json_only: bool
    max_tokens: int

    @property
    def tier(self) -> str:
        return "cheap" if self.model == CHEAP_MODEL else "strong"

    @property
    def user_text(self) -> str:
        """All user-role text in the request, oldest first — what matchers see."""
        return "\n".join(m.text or "" for m in self.messages if m.role == "user")

    @property
    def last_text(self) -> str:
        return (self.messages[-1].text or "") if self.messages else ""

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]

    @property
    def last_tool_result(self) -> str | None:
        last = self.messages[-1] if self.messages else None
        return last.text if last is not None and last.role == "tool_result" else None


@dataclass(slots=True)
class Step:
    """One scripted answer. ``matches`` decides applicability; ``once`` steps
    are consumed, so a two-turn agent exchange is two steps in order."""

    make: Callable[[Request], LLMResult]
    tier: str | None = None
    pattern: re.Pattern[str] | None = None
    needs_tools: bool | None = None
    once: bool = True
    label: str = ""

    def matches(self, req: Request) -> bool:
        if self.tier is not None and req.tier != self.tier:
            return False
        if self.needs_tools is not None and bool(req.tools) != self.needs_tools:
            return False
        if self.pattern is not None and not self.pattern.search(req.user_text):
            return False
        return True


def _result(req: Request, *, text: str = "", tool_calls: list[ToolCall] | None = None,
            stop_reason: str | None = None) -> LLMResult:
    calls = tool_calls or []
    return LLMResult(
        text=text, tool_calls=calls, usage=Usage(in_tokens=50, out_tokens=20),
        model=req.model, provider=PROVIDER_NAME,
        stop_reason=stop_reason or ("tool_use" if calls else "end"),
    )


def _pattern(when: str | None) -> re.Pattern[str] | None:
    return re.compile(when, re.DOTALL) if when else None


class ScriptedProvider:
    """``Provider`` implementation whose answers come from :class:`Step`s."""

    name = PROVIDER_NAME
    supports_tools = True
    supports_vision = True

    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps: list[Step] = list(steps or [])
        self.requests: list[Request] = []
        self.unscripted: list[Request] = []
        self._ids = 0

    # --- authoring helpers ------------------------------------------------

    def triage(self, action: str, *, confidence: float = 0.9, reason: str = "scripted",
               when: str | None = None, once: bool = True) -> ScriptedProvider:
        """Answer the cheap-tier classifier with a verdict."""
        payload = json.dumps({"action": action, "confidence": confidence, "reason": reason})
        self.steps.append(Step(
            make=lambda req: _result(req, text=payload), tier="cheap",
            pattern=_pattern(when), once=once, label=f"triage={action}",
        ))
        return self

    def reply(self, text: str, *, tier: str = "cheap", when: str | None = None,
              once: bool = True) -> ScriptedProvider:
        """A plain text answer (auto-reply drafts, vision descriptions, final text)."""
        self.steps.append(Step(
            make=lambda req: _result(req, text=text), tier=tier,
            pattern=_pattern(when), once=once, label="reply",
        ))
        return self

    def tool_call(self, name: str, *, when: str | None = None, once: bool = True,
                  **args: Any) -> ScriptedProvider:
        """The strong-tier agent decides to call ``name`` with ``args``."""
        def make(req: Request) -> LLMResult:
            self._ids += 1
            return _result(req, tool_calls=[
                ToolCall(id=f"call_{self._ids}", name=name, args=dict(args))
            ])
        self.steps.append(Step(
            make=make, tier="strong", needs_tools=True, pattern=_pattern(when),
            once=once, label=f"tool_call={name}",
        ))
        return self

    def final(self, text: str = "done", *, when: str | None = None,
              once: bool = True) -> ScriptedProvider:
        """The strong-tier agent's closing text after (or instead of) tool calls."""
        return self.reply(text, tier="strong", when=when, once=once)

    def raw(self, make: Callable[[Request], LLMResult], **kw: Any) -> ScriptedProvider:
        self.steps.append(Step(make=make, **kw))
        return self

    # --- the Provider contract -------------------------------------------

    async def complete(self, *, model: str, system: str, messages: list[ChatMessage],
                       tools: list[ToolSpec] | None = None, max_tokens: int = 4096,
                       json_only: bool = False, native_web_search: bool = False) -> LLMResult:
        req = Request(model=model, system=system, messages=list(messages),
                      tools=list(tools or []), json_only=json_only, max_tokens=max_tokens)
        self.requests.append(req)
        for step in self.steps:
            if step.matches(req):
                if step.once:
                    self.steps.remove(step)
                return step.make(req)
        self.unscripted.append(req)
        raise UnscriptedLLMCall(
            f"no scripted answer for a {req.tier} call (tools={bool(req.tools)}); "
            f"last user text: {req.last_text[:160]!r}"
        )

    # --- assertions -------------------------------------------------------

    def calls(self, tier: str | None = None) -> list[Request]:
        return [r for r in self.requests if tier is None or r.tier == tier]

    def offered_tools(self) -> set[str]:
        """Every tool name any request was allowed to call."""
        return {name for r in self.requests for name in r.tool_names}


class FlakyProvider:
    """Raises ``ProviderError`` on every call — the outage the pipeline must
    surface instead of swallowing. ``supports_tools`` mirrors the scripted one
    so the router lets agent runs through to the failure."""

    name = "flaky"
    supports_tools = True
    supports_vision = True

    def __init__(self, message: str = "simulated provider outage") -> None:
        self.message = message
        self.calls = 0

    async def complete(self, **_kw: Any) -> LLMResult:
        self.calls += 1
        raise ProviderError(self.message)
