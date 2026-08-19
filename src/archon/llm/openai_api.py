"""OpenAI backend (chat.completions with function calling + vision)."""

from __future__ import annotations

import base64
import json
from typing import Any

import openai

from .base import ChatMessage, LLMResult, ProviderError, ToolCall, ToolSpec, Usage


def _to_messages(system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "tool_result":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.text or "",
                }
            )
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.args, ensure_ascii=False),
                        },
                    }
                    for tc in m.tool_calls
                ]
            out.append(entry)
        else:
            if m.images:
                content: list[dict[str, Any]] = []
                for img in m.images:
                    b64 = base64.standard_b64encode(img.data).decode()
                    content.append(
                        {"type": "image_url", "image_url": {"url": f"data:{img.mime};base64,{b64}"}}
                    )
                if m.text:
                    content.append({"type": "text", "text": m.text})
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": m.text or ""})
    return out


class OpenAIProvider:
    name = "openai"
    supports_tools = True
    supports_vision = True

    def __init__(self, api_key: str) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)

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
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _to_messages(system, messages),
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        if json_only and not tools:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            raise ProviderError(f"openai API error {exc.status_code}") from exc
        except openai.APIConnectionError as exc:
            raise ProviderError("openai connection error") from exc

        choice = resp.choices[0]
        tool_calls: list[ToolCall] = []
        for tc in choice.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))

        u = resp.usage
        cached = 0
        if u and u.prompt_tokens_details:
            cached = u.prompt_tokens_details.cached_tokens or 0
        return LLMResult(
            text=choice.message.content or "",
            tool_calls=tool_calls,
            usage=Usage(
                in_tokens=(u.prompt_tokens if u else 0) or 0,
                out_tokens=(u.completion_tokens if u else 0) or 0,
                cache_read_tokens=cached,
            ),
            model=resp.model,
            provider=self.name,
            stop_reason="tool_use" if tool_calls else (choice.finish_reason or "end"),
        )
