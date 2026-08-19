"""Anthropic API backend (native tool use + vision).

Current-generation models: no sampling params, adaptive thinking is the
default on claude-opus-5 (do not pass a thinking config), `refusal` is a
possible stop_reason and must be surfaced, tool inputs are parsed objects.
"""

from __future__ import annotations

import base64
from typing import Any

import anthropic

from .base import (
    ChatMessage,
    ImagePart,
    LLMResult,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)


def _content_for(msg: ChatMessage) -> Any:
    parts: list[dict[str, Any]] = []
    for img in msg.images:
        parts.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.mime,
                    "data": base64.standard_b64encode(img.data).decode(),
                },
            }
        )
    if msg.text is not None:
        parts.append({"type": "text", "text": msg.text})
    return parts if (msg.images or not msg.text) else msg.text


def _to_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "tool_result":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.text or "",
                    "is_error": m.is_error,
                }
            )
            continue
        flush_results()
        if m.role == "assistant":
            if m.raw is not None:
                out.append({"role": "assistant", "content": m.raw})
            else:
                blocks: list[dict[str, Any]] = []
                if m.text:
                    blocks.append({"type": "text", "text": m.text})
                for tc in m.tool_calls:
                    blocks.append(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                    )
                out.append({"role": "assistant", "content": blocks or m.text or ""})
        else:
            out.append({"role": "user", "content": _content_for(m)})
    flush_results()
    return out


class AnthropicProvider:
    name = "anthropic"
    supports_tools = True
    supports_vision = True

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_only: bool = False,
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": _to_messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        # json_only relies on the prompt-side instruction; structured outputs
        # would need a fixed schema per call site, which triage supplies itself.
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"anthropic API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("anthropic connection error") from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input)))

        stop = {
            "end_turn": "end",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "refusal": "refusal",
        }.get(resp.stop_reason or "end_turn", resp.stop_reason or "end")

        u = resp.usage
        return LLMResult(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            usage=Usage(
                in_tokens=u.input_tokens,
                out_tokens=u.output_tokens,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            ),
            model=resp.model,
            provider=self.name,
            stop_reason=stop,
            raw_assistant=[b.model_dump() for b in resp.content],
        )
