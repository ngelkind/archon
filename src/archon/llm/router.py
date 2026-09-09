"""Per-purpose, context-aware provider router.

Each call is routed along an ordered PROVIDER CHAIN chosen by (context, purpose,
tools/images present) — see DEFAULT_ROUTES: a private-chat reply ("dm") uses
anthropic/gemini only; tool/agent work uses gemini/anthropic/openrouter; vision
uses a vision-capable provider; everything else (triage, classification, group
replies) uses nvidia then gemini. The first candidate that has a key, the needed
capability, and answers wins. A provider out of balance/quota warns the owner
and the router falls through to the next; an exhausted chain raises. Within a
provider, the cheap/strong tier picks the model. `llm.routes` overrides the
chains and `llm.force_provider` pins one provider (used by the test harness).

Every call is recorded in llm_calls with its cost, and gated by the daily
budget (port of calibot's DAILY_LLM_BUDGET).
"""

from __future__ import annotations

import time

from ..db import repo
from ..runtime import Runtime
from .base import (
    BudgetExhausted,
    ChatMessage,
    LLMResult,
    Provider,
    ProviderError,
    ToolSpec,
    Truncated,
)

# Purposes map to tiers; models resolved per active provider.
_TIER_FOR_PURPOSE = {
    "triage": "cheap",
    "vision": "cheap",
    "reply": "cheap",       # the tool-less per-message "answering agent"
    "agent": "strong",
    "persona_chat": "strong",
    "heavy": "strong",
    "debug": "strong",
}

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {"cheap": "claude-haiku-4-5", "strong": "claude-opus-5"},
    # strong is a flash, not a pro: gemini-3.1-pro 404s (only -preview names exist)
    # and the pro models 429 on a free-tier key. Bump strong to a pro on a paid key.
    # gemini-3.6-flash hit its free-tier quota (429) in testing; the stable
    # "-latest" alias avoids a pinned version drying up. flash-latest does
    # vision + tools; strong stays on 3.7-flash (proven for tool-calls).
    "gemini": {"cheap": "gemini-flash-latest", "strong": "gemini-3.7-flash"},
    "openai": {"cheap": "gpt-5-mini", "strong": "gpt-5"},
    # OpenRouter values are comma-separated in-request fallback chains.
    "openrouter": {
        "cheap": "google/gemini-3.6-flash,deepseek/deepseek-v4-flash",
        "strong": "anthropic/claude-sonnet-5",
    },
    "claude_code": {"cheap": "sonnet", "strong": "sonnet"},
    # NVIDIA build.nvidia.com. nemotron-3-super is what this account reliably
    # serves; with "detailed thinking off" (set in llm/nvidia.py) and enough
    # max_tokens it DOES emit clean tool calls. Its vision model timed out on
    # this account, so vision is routed to gemini instead.
    "nvidia": {"cheap": "nvidia/nemotron-3-super-120b-a12b",
               "strong": "nvidia/nemotron-3-super-120b-a12b"},
}

#: Per-route provider chains, tried in order until one succeeds (the owner's
#: LLM policy). A route is chosen by (context, purpose, whether tools/images are
#: present). Override at runtime with the ``llm.routes`` setting (same shape).
#:
#: - ``dm``  — a private-chat auto-reply, written AS THE OWNER to a real person:
#:   quality/privacy providers only. NEVER openrouter (not private), never nvidia.
#: - ``tool`` — needs native tool-calling: gemini/claude, openrouter as fallback.
#: - ``vision`` — needs an image-capable model (nvidia's vision model is down).
#: - ``default`` — triage / classification / group replies: nvidia, then gemini.
DEFAULT_ROUTES: dict[str, list[str]] = {
    "dm": ["anthropic", "gemini"],
    "tool": ["gemini", "anthropic", "openrouter"],
    "vision": ["gemini", "anthropic"],
    "default": ["nvidia", "gemini"],
}

#: Substrings that mark a provider error as "no balance / credit / quota" — the
#: owner is warned and the router falls through to the next provider (never a
#: silent drop).
_NO_BALANCE = ("insufficient", "quota", "balance", "credit", "payment",
               "402", "billing", "exceeded your current", "out of credit")


def _route_key(purpose: str, has_tools: bool, has_images: bool,
               context: str | None) -> str:
    if context == "dm":
        return "dm"
    if purpose == "vision" or has_images:
        return "vision"
    if has_tools or purpose in ("agent", "heavy", "debug"):
        return "tool"
    return "default"


def _is_no_balance(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(k in s for k in _NO_BALANCE)


class Router:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._providers: dict[str, Provider] = {}

    # --- resolution ---------------------------------------------------------

    def active_provider_name(self) -> str:
        return repo.setting_get(
            self.rt.db, "llm.active_provider", self.rt.settings.llm_active_provider
        )

    def model_for(self, provider: str, purpose: str) -> str:
        tier = _TIER_FOR_PURPOSE.get(purpose, "strong")
        override = repo.setting_get(self.rt.db, f"llm.model.{provider}.{tier}", None)
        if override:
            return str(override)
        return PROVIDER_DEFAULTS.get(provider, {}).get(tier, "")

    def _get_provider(self, name: str) -> Provider:
        if name in self._providers:
            return self._providers[name]
        s = self.rt.settings
        db_key = repo.setting_get(self.rt.db, f"llm.key.{name}", None)
        if name == "anthropic":
            key = db_key or s.anthropic_api_key
            if not key:
                raise ProviderError("no Anthropic API key configured")
            from .anthropic_api import AnthropicProvider
            provider: Provider = AnthropicProvider(key, rt=self.rt)
        elif name == "openai":
            key = db_key or s.openai_api_key
            if not key:
                raise ProviderError("no OpenAI API key configured")
            from .openai_api import OpenAIProvider
            provider = OpenAIProvider(key, rt=self.rt)
        elif name == "gemini":
            key = db_key or s.gemini_api_key
            if not key:
                raise ProviderError("no Gemini API key configured")
            from .gemini import GeminiProvider
            provider = GeminiProvider(key, rt=self.rt)
        elif name == "nvidia":
            key = db_key or s.nvidia_api_key
            if not key:
                raise ProviderError("no NVIDIA API key configured")
            from .nvidia import NvidiaProvider
            provider = NvidiaProvider(key, rt=self.rt)
        elif name == "openrouter":
            key = db_key or s.openrouter_api_key
            if not key:
                raise ProviderError("no OpenRouter API key configured")
            from .openrouter import OpenRouterProvider
            provider = OpenRouterProvider(key, rt=self.rt)
        elif name == "claude_code":
            from .claude_code import ClaudeCodeProvider
            provider = ClaudeCodeProvider()
        else:
            raise ProviderError(f"unknown provider: {name}")
        self._providers[name] = provider
        return provider

    def invalidate(self, name: str | None = None) -> None:
        """Drop cached provider client(s) after a key/provider change."""
        if name:
            self._providers.pop(name, None)
        else:
            self._providers.clear()

    # --- budget ---------------------------------------------------------------

    def _check_budget(self) -> None:
        budget = float(
            repo.setting_get(
                self.rt.db, "llm.daily_budget_usd", self.rt.settings.llm_daily_budget_usd
            )
        )
        if budget <= 0:
            return
        day = repo.llm_cost_since(self.rt.db, "-1 day")
        if day and float(day["cost"]) >= budget:
            raise BudgetExhausted(
                f"daily LLM budget ${budget:.2f} exhausted (${day['cost']:.2f} spent)"
            )

    # --- the call -------------------------------------------------------------

    def provider_chain(self, purpose: str, has_tools: bool, has_images: bool,
                       context: str | None) -> list[str]:
        """The ordered provider candidates for this call. ``llm.routes`` (a
        setting, same shape as DEFAULT_ROUTES) overrides the defaults; a
        non-empty ``llm.force_provider`` setting pins ONE provider for every
        route (a manual override for testing)."""
        forced = repo.setting_get(self.rt.db, "llm.force_provider", None)
        if forced:
            return [str(forced)]
        routes = repo.setting_get(self.rt.db, "llm.routes", None) or DEFAULT_ROUTES
        key = _route_key(purpose, has_tools, has_images, context)
        chain = routes.get(key) or DEFAULT_ROUTES.get(key) or DEFAULT_ROUTES["default"]
        return list(chain)

    async def complete(
        self,
        *,
        purpose: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_only: bool = False,
        native_web_search: bool = False,
        chat_pk: int | None = None,
        context: str | None = None,
    ) -> LLMResult:
        """Route this call along the provider chain for its (context, purpose):
        the first provider that has a key, the needed capability, and answers
        wins. A provider that is out of balance warns the owner and the router
        falls through to the next; only when the whole chain fails does the
        caller see an error. ``context="dm"`` marks a private-chat reply."""
        has_images = any(m.images for m in messages)
        chain = self.provider_chain(purpose, bool(tools), has_images, context)
        errors: list[str] = []
        for name in chain:
            started = time.monotonic()
            # --- pre-flight: is this candidate usable at all? ---
            try:
                provider = self._get_provider(name)  # ProviderError if no key
                if tools and not provider.supports_tools:
                    raise ProviderError(f"{name}: no tool-calling")
                if has_images and not provider.supports_vision:
                    raise ProviderError(f"{name}: no vision")
                if name != "claude_code":
                    self._check_budget()  # global; aborts the whole chain
                model = self.model_for(name, purpose)
                if not model:
                    raise ProviderError(f"{name}: no model for {purpose}")
            except BudgetExhausted as exc:
                repo.llm_call_record(self.rt.db, purpose=purpose, provider=name,
                                     model="", latency_ms=0, ok=False, chat_pk=chat_pk)
                self.rt.audit.note("llm_blocked", provider=name, purpose=purpose,
                                   error="budget exhausted")
                await self._on_failure(exc)
                raise
            except ProviderError as exc:
                errors.append(str(exc))
                continue  # candidate unavailable — next in the chain

            # --- the call ---
            try:
                result = await provider.complete(
                    model=model, system=system, messages=messages, tools=tools,
                    max_tokens=max_tokens, json_only=json_only,
                    native_web_search=native_web_search)
            except ProviderError as exc:
                repo.llm_call_record(
                    self.rt.db, purpose=purpose, provider=name, model=model,
                    latency_ms=int((time.monotonic() - started) * 1000), ok=False,
                    chat_pk=chat_pk)
                if _is_no_balance(exc):
                    await self._warn_balance(name, exc)
                errors.append(f"{name}: {exc}")
                await self._on_failure(exc)
                continue  # this provider failed — try the next

            return self._record_success(result, name, purpose, chat_pk, started)

        # --- the whole chain failed ---
        repo.llm_call_record(self.rt.db, purpose=purpose,
                             provider=(chain[-1] if chain else ""), model="",
                             latency_ms=0, ok=False, chat_pk=chat_pk)
        detail = "; ".join(errors) or "no providers configured for this route"
        self.rt.audit.note("llm_blocked", purpose=purpose, route=",".join(chain),
                           error=detail[:300])
        exc = ProviderError(f"no provider could handle {purpose}: {detail}")
        await self._on_failure(exc)
        raise exc

    def _record_success(self, result: LLMResult, name: str, purpose: str,
                        chat_pk: int | None, started: float) -> LLMResult:
        from .cost import compute_cost
        reported = None
        if isinstance(result.raw_assistant, dict):
            reported = result.raw_assistant.get("openrouter_cost_usd")
        cost = compute_cost(self.rt.db, name, result.model, result.usage,
                            reported, rt=self.rt)
        repo.llm_call_record(
            self.rt.db, purpose=purpose, provider=name, model=result.model,
            in_tokens=result.usage.in_tokens, out_tokens=result.usage.out_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cache_write_tokens=result.usage.cache_write_tokens, cost_usd=cost,
            tool_call_count=len(result.tool_calls),
            latency_ms=int((time.monotonic() - started) * 1000), ok=True,
            chat_pk=chat_pk)
        self.rt.events.publish("cost.update", provider=name, model=result.model,
                               purpose=purpose, cost_usd=cost)
        self._consecutive_failures = 0
        if self.rt.health.get("llm", "").startswith("failing"):
            self.rt.health["llm"] = "ok"
        if result.stop_reason == "refusal":
            raise ProviderError("the model declined this request (safety refusal)")
        if result.stop_reason in ("max_tokens", "length") and not (result.text or "").strip() \
                and not result.tool_calls:
            raise Truncated(f"{name} was cut off ({result.stop_reason}) with no usable output")
        return result

    async def _warn_balance(self, name: str, exc: Exception) -> None:
        from .. import alerts

        self.rt.health[f"llm:{name}"] = "out of balance"
        await alerts.alert_owner(
            self.rt, f"llm_balance:{name}",
            f"⚠️ {name} is out of balance/credit ({str(exc)[:120]}); "
            "routing to the next provider.", every_s=1800)

    async def _on_failure(self, exc: ProviderError) -> None:
        """Track consecutive provider failures; after a few, flag health and
        tell the owner — the router docstring promised this and nothing did it."""
        self._consecutive_failures = getattr(self, "_consecutive_failures", 0) + 1
        self.rt.health["llm"] = f"failing: {type(exc).__name__}"
        if isinstance(exc, BudgetExhausted) or self._consecutive_failures >= 3:
            from .. import alerts

            key = "llm:budget" if isinstance(exc, BudgetExhausted) else "llm:failing"
            await alerts.alert_owner(
                self.rt, key,
                f"\u26a0\ufe0f LLM calls are failing ({type(exc).__name__}): "
                f"{str(exc)[:200]}. Inbound triage and replies are affected.",
                every_s=3600 if isinstance(exc, BudgetExhausted) else 1800)
