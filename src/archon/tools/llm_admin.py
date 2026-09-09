"""LLM administration and cost tools (owner scope only)."""

from __future__ import annotations

import json

from ..db import repo
from ..llm.router import PROVIDER_DEFAULTS, Router
from .registry import Registry, ToolContext


def register(registry: Registry) -> None:
    @registry.tool(
        "llm_set_provider",
        "Switch the single active LLM provider. One of: anthropic, openai, gemini, "
        "openrouter, claude_code. The provider's key must already be configured "
        "(env or llm_set_key).",
        {
            "type": "object",
            "properties": {"provider": {
                "type": "string",
                "enum": ["anthropic", "openai", "gemini", "openrouter", "claude_code"],
            }},
            "required": ["provider"],
        },
        sensitive=True,
    )
    async def llm_set_provider(ctx: ToolContext, provider: str) -> str:
        router: Router = ctx.rt.router  # type: ignore[assignment]
        router.invalidate()
        router._get_provider(provider)  # validates the key exists; raises if not
        repo.setting_set(ctx.store, "llm.active_provider", provider)
        return json.dumps({"ok": True, "active_provider": provider})

    @registry.tool(
        "llm_get_routing",
        "Show the active provider, its cheap/strong models, and the daily budget.",
    )
    async def llm_get_routing(ctx: ToolContext) -> str:
        from ..llm.router import DEFAULT_ROUTES

        router: Router = ctx.rt.router  # type: ignore[assignment]
        routes = repo.setting_get(ctx.store, "llm.routes", None) or DEFAULT_ROUTES
        forced = repo.setting_get(ctx.store, "llm.force_provider", None)
        return json.dumps({
            "routes": routes,  # dm / tool / vision / default -> provider chains
            "force_provider": forced or None,
            "models_per_provider": {
                p: {"cheap": router.model_for(p, "triage"),
                    "strong": router.model_for(p, "agent")}
                for p in ("nvidia", "gemini", "anthropic", "openrouter")
            },
            "daily_budget_usd": repo.setting_get(
                ctx.store, "llm.daily_budget_usd", ctx.rt.settings.llm_daily_budget_usd),
        })

    @registry.tool(
        "llm_set_model",
        "Override the model used for a tier (cheap = triage/vision, strong = "
        "agent/heavy) of a provider. Empty model resets to the default.",
        {
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "tier": {"type": "string", "enum": ["cheap", "strong"]},
                "model": {"type": "string"},
            },
            "required": ["provider", "tier", "model"],
        },
        sensitive=True,
    )
    async def llm_set_model(ctx: ToolContext, provider: str, tier: str, model: str) -> str:
        key = f"llm.model.{provider}.{tier}"
        if model.strip():
            repo.setting_set(ctx.store, key, model.strip())
        else:
            repo.setting_set(ctx.store, key, None)
        return json.dumps({"ok": True, "provider": provider, "tier": tier,
                           "model": model or PROVIDER_DEFAULTS.get(provider, {}).get(tier)})

    @registry.tool(
        "llm_set_key",
        "Store or replace an API key for a provider (kept in the local database, "
        "never in git). Also switches nothing — use llm_set_provider after.",
        {
            "type": "object",
            "properties": {
                "provider": {"type": "string",
                             "enum": ["anthropic", "openai", "gemini", "openrouter"]},
                "api_key": {"type": "string"},
            },
            "required": ["provider", "api_key"],
        },
        sensitive=True,
    )
    async def llm_set_key(ctx: ToolContext, provider: str, api_key: str) -> str:
        repo.setting_set(ctx.store, f"llm.key.{provider}", api_key.strip())
        router: Router = ctx.rt.router  # type: ignore[assignment]
        router.invalidate(provider)
        return json.dumps({"ok": True, "provider": provider, "key_len": len(api_key.strip())})

    @registry.tool(
        "budget_set",
        "Set the daily LLM budget in USD (0 disables the gate).",
        {
            "type": "object",
            "properties": {"usd_per_day": {"type": "number"}},
            "required": ["usd_per_day"],
        },
        sensitive=True,
    )
    async def budget_set(ctx: ToolContext, usd_per_day: float) -> str:
        repo.setting_set(ctx.store, "llm.daily_budget_usd", float(usd_per_day))
        return json.dumps({"ok": True, "daily_budget_usd": float(usd_per_day)})

    @registry.tool(
        "cost_report",
        "LLM spend summary for the last day, week, and month.",
    )
    async def cost_report(ctx: ToolContext) -> str:
        out = {}
        for label, expr in (("day", "-1 day"), ("week", "-7 days"), ("month", "-30 days")):
            row = repo.llm_cost_since(ctx.store, expr)
            out[label] = {
                "cost_usd": round(float(row["cost"]), 4),
                "calls": row["calls"],
                "in_tokens": row["in_tok"],
                "out_tokens": row["out_tok"],
            } if row else {}
        return json.dumps(out)

    @registry.tool(
        "cost_breakdown",
        "LLM spend grouped by provider, model, and purpose over a period.",
        {
            "type": "object",
            "properties": {"period": {"type": "string", "enum": ["day", "week", "month"]}},
            "required": ["period"],
        },
    )
    async def cost_breakdown(ctx: ToolContext, period: str) -> str:
        spans = {"day": "-1 day", "week": "-7 days", "month": "-30 days"}
        expr = spans.get(period)
        if expr is None:
            # A bad value used to raise KeyError (a handler "bug"); return a
            # clean, actionable error the model can correct instead.
            return json.dumps({"error": f"period must be one of {sorted(spans)}",
                               "got": period})
        rows = repo.llm_cost_breakdown(ctx.store, expr)
        return json.dumps([
            {"provider": r["provider"], "model": r["model"], "purpose": r["purpose"],
             "calls": r["calls"], "cost_usd": round(float(r["cost"]), 4)}
            for r in rows
        ])
