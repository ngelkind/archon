"""Gemini backend via the official google-genai SDK (function calling + vision)."""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import types as gt

from .base import ChatMessage, LLMResult, ProviderError, ToolCall, ToolSpec, Usage

# Gemini's function-declaration schema is a subset of JSON Schema; strip keys
# it rejects rather than failing the whole call.
_ALLOWED_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "items",
    "properties", "required", "anyOf",
}


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in _ALLOWED_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _clean_schema(s) for name, s in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _clean_schema(v)
        elif k == "anyOf" and isinstance(v, list):
            out[k] = [_clean_schema(s) for s in v]
        else:
            out[k] = v
    return out


def _to_contents(messages: list[ChatMessage]) -> list[gt.Content]:
    contents: list[gt.Content] = []
    for m in messages:
        if m.role == "tool_result":
            contents.append(
                gt.Content(
                    role="user",
                    parts=[
                        gt.Part.from_function_response(
                            name=m.tool_name or "tool",
                            response={"result": m.text or "", "is_error": m.is_error},
                        )
                    ],
                )
            )
        elif m.role == "assistant":
            parts: list[gt.Part] = []
            if m.text:
                parts.append(gt.Part.from_text(text=m.text))
            for tc in m.tool_calls:
                parts.append(gt.Part.from_function_call(name=tc.name, args=tc.args))
            contents.append(gt.Content(role="model", parts=parts or [gt.Part.from_text(text="")]))
        else:
            parts = []
            for img in m.images:
                parts.append(gt.Part.from_bytes(data=img.data, mime_type=img.mime))
            if m.text:
                parts.append(gt.Part.from_text(text=m.text))
            contents.append(gt.Content(role="user", parts=parts))
    return contents


class GeminiProvider:
    name = "gemini"
    supports_tools = True
    supports_vision = True

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

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
        config = gt.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )
        if tools:
            config.tools = [
                gt.Tool(
                    function_declarations=[
                        gt.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters=_clean_schema(t.input_schema),
                        )
                        for t in tools
                    ]
                )
            ]
        elif json_only:
            config.response_mime_type = "application/json"

        try:
            resp = await self._client.aio.models.generate_content(
                model=model, contents=_to_contents(messages), config=config
            )
        except Exception as exc:  # SDK raises assorted google.* error types
            raise ProviderError(f"gemini API error: {type(exc).__name__}") from exc

        text = ""
        tool_calls: list[ToolCall] = []
        try:
            candidate = resp.candidates[0] if resp.candidates else None
            for i, part in enumerate((candidate.content.parts or []) if candidate else []):
                if part.text:
                    text += part.text
                if part.function_call:
                    tool_calls.append(
                        ToolCall(
                            id=f"gm-{i}",
                            name=part.function_call.name or "",
                            args=dict(part.function_call.args or {}),
                        )
                    )
        except (AttributeError, IndexError) as exc:
            raise ProviderError("gemini returned an unexpected response shape") from exc

        um = resp.usage_metadata
        return LLMResult(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(
                in_tokens=(um.prompt_token_count if um else 0) or 0,
                out_tokens=(um.candidates_token_count if um else 0) or 0,
                cache_read_tokens=(um.cached_content_token_count if um else 0) or 0,
            ),
            model=model,
            provider=self.name,
            stop_reason="tool_use" if tool_calls else "end",
        )
