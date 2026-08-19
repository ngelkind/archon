"""Tool registry: the agent's contract with the world.

Every capability is a registered tool. Scoping is the security boundary:

- scope "owner": callable only in owner-initiated control-bot sessions
  (settings, whitelists, sub-bots, LLM admin, sends to arbitrary chats).
- scope "inbound": additionally callable on agent runs triggered by inbound
  platform content. These runs get NO admin tools, and sends are forced
  through the confirm gate by the send tools themselves.

Handlers are async callables (ctx: ToolContext, **args) -> str. Results are
strings (JSON for structured data) because that is what goes back to the
model verbatim.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..llm.base import ToolSpec
from ..runtime import Runtime

Scope = Literal["owner", "inbound"]


@dataclass(slots=True)
class ToolContext:
    rt: Runtime
    scope: Scope
    # For inbound-triggered runs: the chat that triggered the run. Send tools
    # in inbound scope may only target this chat.
    origin_chat_pk: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    scopes: frozenset[str]
    # Tools that mutate state or send messages; audited with args.
    sensitive: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description,
                        input_schema=self.input_schema)


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None = None,
        scopes: tuple[str, ...] = ("owner",),
        sensitive: bool = False,
    ):
        """Decorator: @registry.tool("wa_send_message", "...", {...})."""

        def deco(fn: Callable[..., Awaitable[str]]):
            schema = input_schema or {"type": "object", "properties": {}, "required": []}
            self.add(Tool(
                name=name, description=description, input_schema=schema,
                handler=fn, scopes=frozenset(scopes), sensitive=sensitive,
            ))
            return fn

        return deco

    def specs_for(self, scope: Scope) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values() if scope in t.scopes]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def dispatch(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        if ctx.scope not in tool.scopes:
            ctx.rt.audit.tool(name=name, args=args, ok=False,
                              result_summary="denied: out of scope")
            return json.dumps({"error": f"tool {name} is not available in this context"})
        try:
            sig = inspect.signature(tool.handler)
            has_var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            accepted = dict(args) if has_var_kw else {
                k: v for k, v in args.items() if k in sig.parameters
            }
            result = await tool.handler(ctx, **accepted)
            ctx.rt.audit.tool(name=name, args=args if tool.sensitive else {},
                              ok=True, result_summary=str(result)[:200])
            return result
        except Exception as exc:  # noqa: BLE001 — errors go back to the model
            ctx.rt.audit.tool(name=name, args=args, ok=False,
                              result_summary=repr(exc)[:200])
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
