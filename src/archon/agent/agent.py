"""Provider-agnostic tool loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..llm.base import ChatMessage, ProviderError
from ..llm.router import Router
from ..tools.registry import Registry, ToolContext

_MAX_ITERATIONS = 8


@dataclass(slots=True)
class ToolCallEvent:
    """Emitted just before a tool is dispatched."""

    name: str
    call_id: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResultEvent:
    """Emitted just after a tool returns its (string) result."""

    name: str
    call_id: str
    result: str


AgentEvent = ToolCallEvent | ToolResultEvent
OnEvent = Callable[[AgentEvent], Awaitable[None]]


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
    on_event: OnEvent | None = None,
) -> str:
    """Run the tool loop until the model stops calling tools; return final text.

    ``on_event`` (optional) receives a :class:`ToolCallEvent` before each
    dispatch and a :class:`ToolResultEvent` after — used by streaming transports
    (the API) to surface progress. Existing callers pass nothing and are
    unaffected.
    """
    tools = registry.specs_for(ctx.scope, hidden=registry.hidden_for(ctx.rt))
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
            if on_event is not None:
                await on_event(ToolCallEvent(name=call.name, call_id=call.id, args=call.args))
            output = await registry.dispatch(ctx, call.name, call.args)
            if on_event is not None:
                await on_event(ToolResultEvent(name=call.name, call_id=call.id, result=output))
            history.append(
                ChatMessage(
                    role="tool_result",
                    text=output,
                    tool_call_id=call.id,
                    tool_name=call.name,
                )
            )

    raise ProviderError(f"agent exceeded {_MAX_ITERATIONS} tool iterations")
