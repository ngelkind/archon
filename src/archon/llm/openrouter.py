"""OpenRouter backend (OpenAI-shaped chat/completions over httpx).

Carries over two things from your-other-project's provider:
``data_collection: deny`` on every request (paid endpoints that don't train
on prompts), and in-request model fallback via the ``models`` array.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from .base import ChatMessage, LLMResult, ProviderError, ToolCall, ToolSpec, Usage

_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider:
    name = "openrouter"
    supports_tools = True
    supports_vision = True

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=120)

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
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "tool_result":
                msgs.append(
                    {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text or ""}
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
                msgs.append(entry)
            else:
                if m.images:
                    content: list[dict[str, Any]] = []
                    for img in m.images:
                        b64 = base64.standard_b64encode(img.data).decode()
                        content.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{img.mime};base64,{b64}"},
                            }
                        )
                    if m.text:
                        content.append({"type": "text", "text": m.text})
                    msgs.append({"role": "user", "content": content})
                else:
                    msgs.append({"role": "user", "content": m.text or ""})

        # "model,other-model" in config becomes OpenRouter's in-request fallback.
        model_list = [x.strip() for x in model.split(",") if x.strip()]
        body: dict[str, Any] = {
            "model": model_list[0],
            "messages": msgs,
            "max_tokens": max_tokens,
            "provider": {"data_collection": "deny"},
            "usage": {"include": True},
        }
        if len(model_list) > 1:
            body["models"] = model_list
        if tools:
            body["tools"] = [
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
            body["response_format"] = {"type": "json_object"}

        try:
            resp = await self._client.post(
                _URL,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "X-Title": "Archon",
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("openrouter connection error") from exc
        if resp.status_code != 200:
            raise ProviderError(f"openrouter API error {resp.status_code}")
        data = resp.json()
        if "choices" not in data or not data["choices"]:
            raise ProviderError("openrouter returned no choices")

        choice = data["choices"][0]
        message = choice.get("message", {})
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            try:
                args = json.loads(tc.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=tc.get("function", {}).get("name", ""), args=args)
            )

        usage = data.get("usage") or {}
        # OpenRouter returns actual cost when usage.include is set.
        cost = float(usage.get("cost") or 0.0)
        result = LLMResult(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            usage=Usage(
                in_tokens=int(usage.get("prompt_tokens") or 0),
                out_tokens=int(usage.get("completion_tokens") or 0),
            ),
            model=data.get("model", model_list[0]),
            provider=self.name,
            stop_reason="tool_use" if tool_calls else (choice.get("finish_reason") or "end"),
        )
        # Stash provider-reported USD cost for the router's recorder.
        result.raw_assistant = {"openrouter_cost_usd": cost}
        return result
