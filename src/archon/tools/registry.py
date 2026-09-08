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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..llm.base import ToolSpec
from ..runtime import Runtime

Scope = Literal["owner", "inbound"]

#: Argument names whose VALUE must never reach the audit log, even for a
#: sensitive tool that otherwise logs its args (llm_set_key wrote a plaintext
#: provider key to audit.jsonl and the DB mirror).
_SECRET_ARGS = frozenset({"api_key", "token", "secret", "password", "session"})
#: Exceptions that are always a programming bug in a handler, never bad user
#: input — worth an owner-facing signal, not just a JSON string to the model.
_BUG_ERRORS = (AttributeError, TypeError, KeyError, NameError, IndexError)


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    return {k: ("•••" if k in _SECRET_ARGS else v) for k, v in args.items()}


def _with_warning(result: str, warning: str) -> str:
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            data["warning"] = warning
            return json.dumps(data, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return json.dumps({"result": result, "warning": warning}, ensure_ascii=False)


@dataclass(slots=True)
class ToolContext:
    rt: Runtime
    scope: Scope
    # For inbound-triggered runs: the chat that triggered the run. Send tools
    # in inbound scope may only target this chat.
    origin_chat_pk: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    # Whose data this run acts on. None means the single-user owner, which is
    # what every legacy caller gets. Set it and `store` follows, so a tool
    # written against `ctx.store` is automatically tenant-correct.
    tenant: Any = None

    @property
    def tenant_id(self) -> int:
        """Whose data this run acts on; the owner when no tenant is attached."""
        from ..db.tenancy import OWNER_TENANT_ID

        return self.tenant.tenant_id if self.tenant is not None else OWNER_TENANT_ID

    @property
    def store(self):
        """The database handle a tool should use: this run's tenant scope, or
        the raw Db (owner-scoped by repo) when no tenant is attached."""
        return self.tenant.scope if self.tenant is not None else self.rt.db


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

    def specs_for(self, scope: Scope, *, hidden: frozenset[str] = frozenset()) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()
                if scope in t.scopes and t.name not in hidden]

    def hidden_for(self, rt: Any) -> frozenset[str]:
        """Tools the agent must not be offered right now — the WhatsApp tools
        when the subsystem is switched off (they could only fail)."""
        from ..platforms.whatsapp import client as wa_client

        if wa_client.enabled(rt):
            return frozenset()
        return frozenset(name for name in self._tools if name.startswith("wa_"))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def dispatch(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        if ctx.scope not in tool.scopes:
            ctx.rt.audit.tool(name=name, args=args, ok=False,
                              result_summary="denied: out of scope",
                              tenant_id=ctx.tenant_id)
            return json.dumps({"error": f"tool {name} is not available in this context"})
        audit_args = _redact(args) if tool.sensitive else {}
        try:
            sig = inspect.signature(tool.handler)
            has_var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if has_var_kw:
                accepted, dropped = dict(args), []
            else:
                accepted = {k: v for k, v in args.items() if k in sig.parameters}
                dropped = [k for k in args if k not in sig.parameters]
            result = await tool.handler(ctx, **accepted)
            if dropped:
                # An argument the model supplied that the handler does not take
                # was silently discarded — which quietly changes the call's
                # meaning (a dropped `schedule` becomes an immediate send). Tell
                # the model and the audit instead of hiding it.
                ctx.rt.audit.tool(name=name, args={**audit_args, "_ignored": dropped},
                                  ok=True, result_summary=str(result)[:200],
                                  tenant_id=ctx.tenant_id)
                return _with_warning(result, f"ignored unknown argument(s): {dropped}")
            ctx.rt.audit.tool(name=name, args=audit_args, ok=True,
                              result_summary=str(result)[:200], tenant_id=ctx.tenant_id)
            return result
        except _BUG_ERRORS as exc:
            # A programming bug in a handler, not user error. The model still
            # gets a string, but the owner is told and health degrades so a
            # dead tool is not invisible behind a JSON error only the model sees.
            ctx.rt.audit.note("tool_bug", tool=name, error=repr(exc)[:200],
                              tenant_id=ctx.tenant_id)
            ctx.rt.health[f"tool:{name}"] = f"bug: {type(exc).__name__}"
            ctx.rt.audit.tool(name=name, args=_redact(args), ok=False,
                              result_summary=repr(exc)[:200], tenant_id=ctx.tenant_id)
            from .. import alerts

            await alerts.alert_owner(
                ctx.rt, f"tool_bug:{name}",
                f"⚠️ Tool {name} hit a bug: {type(exc).__name__}: {str(exc)[:150]}")
            return json.dumps({"error": f"{type(exc).__name__}: {exc}", "is_error": True})
        except Exception as exc:  # noqa: BLE001 — user-facing errors go back to the model
            ctx.rt.audit.tool(name=name, args=_redact(args), ok=False,
                              result_summary=repr(exc)[:200], tenant_id=ctx.tenant_id)
            return json.dumps({"error": f"{type(exc).__name__}: {exc}", "is_error": True})
