"""Price table and cost computation.

Prices are USD per million tokens: (input, output, cache_read, cache_write).
Snapshot of 2026-08 public pricing; runtime-overridable via the settings key
``llm.prices`` (same structure, merged over the defaults) so a price change
never requires a deploy. OpenRouter reports its own USD cost per response,
which takes precedence. claude_code is subscription: always $0, still tracked.
"""

from __future__ import annotations

from typing import Any

from ..db import Db
from ..db.repo import setting_get
from .base import Usage

# (in, out, cache_read, cache_write) per MTok. Longest-prefix match on model.
PRICES: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "anthropic": {
        "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
        "claude-opus-4": (5.0, 25.0, 0.5, 6.25),
        "claude-sonnet-5": (3.0, 15.0, 0.3, 3.75),
        "claude-sonnet-4": (3.0, 15.0, 0.3, 3.75),
        "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
    },
    "gemini": {
        "gemini-3.7-flash": (0.75, 3.75, 0.075, 0.0),
        "gemini-3.6-flash": (0.75, 3.75, 0.075, 0.0),
        "gemini-3.1-pro": (2.0, 12.0, 0.2, 0.0),
    },
    "openai": {
        "gpt-5-mini": (0.25, 2.0, 0.025, 0.0),
        "gpt-5-nano": (0.05, 0.4, 0.005, 0.0),
        "gpt-5": (1.25, 10.0, 0.125, 0.0),
    },
    "openrouter": {},   # actual cost comes back on each response
    "claude_code": {},  # subscription: $0
}


def _table(db: Db | None) -> dict[str, dict[str, tuple[float, float, float, float]]]:
    if db is None:
        return PRICES
    override: dict[str, Any] = setting_get(db, "llm.prices", {}) or {}
    if not override:
        return PRICES
    merged = {p: dict(models) for p, models in PRICES.items()}
    for provider, models in override.items():
        merged.setdefault(provider, {})
        for model, quad in models.items():
            merged[provider][model] = tuple(quad)  # type: ignore[assignment]
    return merged


def compute_cost(
    db: Db | None, provider: str, model: str, usage: Usage,
    reported_usd: float | None = None,
) -> float:
    if provider == "claude_code":
        return 0.0
    if reported_usd is not None:
        return reported_usd
    models = _table(db).get(provider, {})
    match: tuple[float, float, float, float] | None = None
    best_len = -1
    for prefix, quad in models.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            match, best_len = quad, len(prefix)
    if match is None:
        return 0.0  # unknown model: recorded with zero cost, visible in reports
    in_p, out_p, cr_p, cw_p = match
    return (
        usage.in_tokens * in_p
        + usage.out_tokens * out_p
        + usage.cache_read_tokens * cr_p
        + usage.cache_write_tokens * cw_p
    ) / 1_000_000
