"""Provider-agnostic tool loop."""

from __future__ import annotations

from ..llm.base import ChatMessage, ProviderError
from ..llm.router import Router
from ..tools.registry import Registry, ToolContext

_MAX_ITERATIONS = 8


async def run_agent(
    router: Router,
    registry: Registry,
    ctx: ToolContext,
    *,
    system: str,
    messages: list[ChatMessage],
    purpose: str = "agent",
    chat_pk: int | None = None,
    max_tokens: int = 4096,
) -> str:
    """Run the tool loop until the model stops calling tools; return final text."""
    tools = registry.specs_for(ctx.scope)
    history = list(messages)

    for _ in range(_MAX_ITERATIONS):
        result = await router.complete(
            purpose=purpose,
            system=system,
            messages=history,
            tools=tools,
            max_tokens=max_tokens,
            chat_pk=chat_pk,
        )
        if not result.tool_calls:
            return result.text

        history.append(
            ChatMessage(
                role="assistant",
                text=result.text or None,
                tool_calls=result.tool_calls,
                raw=result.raw_assistant if result.provider == "anthropic" else None,
            )
        )
        for call in result.tool_calls:
            output = await registry.dispatch(ctx, call.name, call.args)
            history.append(
                ChatMessage(
                    role="tool_result",
                    text=output,
                    tool_call_id=call.id,
                    tool_name=call.name,
                )
            )

    raise ProviderError(f"agent exceeded {_MAX_ITERATIONS} tool iterations")
