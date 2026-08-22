"""Provider-neutral LLM types.

Two rules carried over from your-other-project/providers/base.py:

1. **Platform text is untrusted.** It is always passed as user content, never
   concatenated into the system prompt, and always wrapped in
   ``<untrusted_content>`` tags with the closing tag neutralised, so a
   correspondent cannot end the block early and issue instructions.
2. **Message content never reaches a traceback.** Provider failures raise
   :class:`ProviderError` with a short, content-free description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

OPEN_TAG = "<untrusted_content>"
CLOSE_TAG = "</untrusted_content>"

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-]{1,100}$")


class ProviderError(Exception):
    """A provider failed. The message must never contain user content."""


def validate_model(model: str) -> str:
    # Model names reach subprocess argv (claude_code) — validate even though
    # they come from config, not from chat correspondents.
    model = model.strip()
    if not model or not _MODEL_RE.match(model):
        raise ProviderError(f"invalid model name: {model!r}")
    return model


def wrap_untrusted(text: str) -> str:
    """Wrap third-party text so it cannot escape its delimiter."""
    neutralised = text.replace(CLOSE_TAG, "&lt;/untrusted_content&gt;")
    return f"{OPEN_TAG}\n{neutralised}\n{CLOSE_TAG}"


@dataclass(slots=True)
class ImagePart:
    data: bytes
    mime: str  # image/jpeg, image/png, image/webp, image/gif


@dataclass(slots=True)
class ChatMessage:
    role: Literal["user", "assistant", "tool_result"]
    text: str | None = None
    images: list[ImagePart] = field(default_factory=list)
    # role == "assistant" with tool calls the model made previously
    tool_calls: list[ToolCall] = field(default_factory=list)
    # role == "tool_result": result for a specific prior call
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    # Provider-native payload for this message (e.g. Anthropic content blocks
    # incl. thinking blocks, which must be replayed verbatim in a tool loop).
    # When set, providers send it as-is instead of reconstructing from the
    # neutral fields. Only valid within a single run on a single provider.
    raw: Any = None


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema (object)


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    # Opaque provider data that must be echoed back on the follow-up request.
    # Gemini 3.x "thinking" models return a thought_signature on the function
    # call and reject the next turn with 400 if it is not replayed. Other
    # providers leave this None. In-memory for the tool loop only (context is
    # persisted as text, so it never needs to survive a save/load).
    signature: bytes | None = None


@dataclass(slots=True)
class Usage:
    in_tokens: int = 0
    out_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(slots=True)
class LLMResult:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    provider: str
    stop_reason: str  # 'end' | 'tool_use' | 'max_tokens' | 'refusal' | other
    # Provider-native assistant message payload, passed back verbatim on the
    # next turn of a tool loop (Anthropic content blocks etc.). Opaque.
    raw_assistant: Any = None


class Provider(Protocol):
    name: str
    supports_tools: bool
    supports_vision: bool

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_only: bool = False,
        native_web_search: bool = False,
    ) -> LLMResult: ...
