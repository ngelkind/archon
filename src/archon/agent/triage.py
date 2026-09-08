"""Cheap-model triage: does this inbound message need calendar/response work?"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..llm.base import ChatMessage, ProviderError, wrap_untrusted
from ..llm.router import Router
from .prompts import TRIAGE_SYSTEM

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(slots=True)
class TriageResult:
    action: str  # ignore | calendar | respond | both | error
    confidence: float
    reason: str

    @property
    def failed(self) -> bool:
        """The LLM could not be reached/parsed — NOT a real "nothing to do"."""
        return self.action == "error"


async def triage(
    router: Router,
    *,
    platform: str,
    chat_name: str,
    sender_name: str,
    text: str,
    chat_pk: int | None = None,
    auto_reply: bool = False,
) -> TriageResult:
    context = (
        f"Platform: {platform}\nChat: {chat_name}\nSender: {sender_name}\n"
        f"Chat auto-reply enabled: {auto_reply}\n\nMessage(s):\n{wrap_untrusted(text)}"
    )
    try:
        result = await router.complete(
            purpose="triage",
            system=TRIAGE_SYSTEM,
            messages=[ChatMessage(role="user", text=context)],
            max_tokens=200,
            json_only=True,
            chat_pk=chat_pk,
        )
    except ProviderError as exc:
        # The message is already cached; surface the outage rather than
        # silently filing it as "ignore" (the failure mode that let inbound
        # triage be 100% dead while every log line looked normal).
        return TriageResult(action="error", confidence=0.0,
                            reason=f"triage unavailable: {type(exc).__name__}: {str(exc)[:150]}")

    match = _JSON_RE.search(result.text or "")
    if not match:
        return TriageResult(action="error", confidence=0.0,
                            reason="unparseable triage output")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return TriageResult(action="error", confidence=0.0, reason="invalid triage JSON")

    action = str(data.get("action", "ignore"))
    if action not in {"ignore", "calendar", "respond", "both"}:
        action = "ignore"
    if action in {"respond", "both"} and not auto_reply:
        action = "calendar" if action == "both" else "ignore"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return TriageResult(action=action, confidence=confidence,
                        reason=str(data.get("reason", ""))[:200])
