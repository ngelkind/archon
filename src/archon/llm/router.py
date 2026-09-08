"""Single-active-provider router.

Exactly ONE provider is active at a time (`llm.active_provider` setting; the
env var is only the first-boot default). Roles map to a cheap or strong model
tier WITHIN the active provider. Fallbacks stay within the provider (model
level); on total provider failure the caller gets a ProviderError and the
owner is alerted — never a silent switch to a provider whose key may not
exist.

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
    "gemini": {"cheap": "gemini-3.6-flash", "strong": "gemini-3.7-flash"},
    "openai": {"cheap": "gpt-5-mini", "strong": "gpt-5"},
    # OpenRouter values are comma-separated in-request fallback chains.
    "openrouter": {
        "cheap": "google/gemini-3.6-flash,deepseek/deepseek-v4-flash",
        "strong": "anthropic/claude-sonnet-5",
    },
    "claude_code": {"cheap": "sonnet", "strong": "sonnet"},
}


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
            provider: Provider = AnthropicProvider(key)
        elif name == "openai":
            key = db_key or s.openai_api_key
            if not key:
                raise ProviderError("no OpenAI API key configured")
            from .openai_api import OpenAIProvider
            provider = OpenAIProvider(key)
        elif name == "gemini":
            key = db_key or s.gemini_api_key
            if not key:
                raise ProviderError("no Gemini API key configured")
            from .gemini import GeminiProvider
            provider = GeminiProvider(key)
        elif name == "openrouter":
            key = db_key or s.openrouter_api_key
            if not key:
                raise ProviderError("no OpenRouter API key configured")
            from .openrouter import OpenRouterProvider
            provider = OpenRouterProvider(key)
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
    ) -> LLMResult:
        name = self.active_provider_name()
        started = time.monotonic()
        # Pre-flight failures (missing key/model, tool-less provider, budget)
        # used to raise before any llm_calls row was written, so a bot that was
        # quietly refusing every call left no trace. Record and surface them.
        try:
            provider = self._get_provider(name)
            if tools and not provider.supports_tools:
                raise ProviderError(
                    f"active provider {name} does not support tool calling; "
                    "switch provider (llm_set_provider) for agent features"
                )
            if not provider.supports_vision and any(m.images for m in messages):
                raise ProviderError(
                    f"active provider {name} cannot see images; the picture "
                    "would be silently dropped. Switch provider (llm_set_provider) "
                    "to one with vision for this content."
                )
            if name != "claude_code":
                self._check_budget()
            model = self.model_for(name, purpose)
            if not model:
                raise ProviderError(f"no model configured for {name}/{purpose}")
        except ProviderError as exc:
            repo.llm_call_record(
                self.rt.db, purpose=purpose, provider=name, model="",
                latency_ms=0, ok=False, chat_pk=chat_pk,
            )
            self.rt.audit.note("llm_blocked", provider=name, purpose=purpose,
                               error=repr(exc)[:200])
            await self._on_failure(exc)
            raise

        try:
            result = await provider.complete(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                json_only=json_only,
                native_web_search=native_web_search,
            )
        except ProviderError as exc:
            repo.llm_call_record(
                self.rt.db, purpose=purpose, provider=name, model=model,
                latency_ms=int((time.monotonic() - started) * 1000), ok=False,
                chat_pk=chat_pk,
            )
            await self._on_failure(exc)
            raise

        from .cost import compute_cost
        reported = None
        if isinstance(result.raw_assistant, dict):
            reported = result.raw_assistant.get("openrouter_cost_usd")
        cost = compute_cost(self.rt.db, name, result.model, result.usage,
                            reported, rt=self.rt)
        repo.llm_call_record(
            self.rt.db,
            purpose=purpose,
            provider=name,
            model=result.model,
            in_tokens=result.usage.in_tokens,
            out_tokens=result.usage.out_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cache_write_tokens=result.usage.cache_write_tokens,
            cost_usd=cost,
            tool_call_count=len(result.tool_calls),
            latency_ms=int((time.monotonic() - started) * 1000),
            ok=True,
            chat_pk=chat_pk,
        )
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
