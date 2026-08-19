"""Per-chat reply-delay policies.

delay_policy_json on a chat row:
    null / {"mode": "none"}                          → send immediately
    {"mode": "fixed", "min_s": 300}                  → send after exactly 5m
    {"mode": "random", "min_s": 3600, "max_s": 18000} → uniform 1h–5h
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta


def parse_policy(delay_policy_json: str | None) -> dict:
    if not delay_policy_json:
        return {"mode": "none"}
    try:
        policy = json.loads(delay_policy_json)
    except json.JSONDecodeError:
        return {"mode": "none"}
    if not isinstance(policy, dict) or policy.get("mode") not in ("none", "fixed", "random"):
        return {"mode": "none"}
    return policy


def compute_due(delay_policy_json: str | None, now: datetime | None = None) -> datetime | None:
    """None → send immediately; datetime → queue until then."""
    policy = parse_policy(delay_policy_json)
    now = now or datetime.now(UTC)
    mode = policy["mode"]
    if mode == "none":
        return None
    lo = max(0.0, float(policy.get("min_s", 0)))
    if mode == "fixed":
        return now + timedelta(seconds=lo)
    hi = max(lo, float(policy.get("max_s", lo)))
    return now + timedelta(seconds=random.uniform(lo, hi))
