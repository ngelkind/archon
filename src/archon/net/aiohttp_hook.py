"""aiohttp tracing that records Bot API traffic to the ledger.

aiogram talks to the Telegram Bot API over aiohttp. We attach an aiohttp
``TraceConfig`` to the session aiogram builds, so every Bot API call (a control
reply, a log card, an alert, a media upload) lands in the ledger without any
call site knowing. Telethon's MTProto is raw sockets, not aiohttp — that traffic
is the socket probe's job, not this hook's.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import TraceConfig

from ..netlog import NetCall

_MARK = "_archon_ledger_trace"


def make_trace_config(rt: Any, subsystem: str = "telegram") -> TraceConfig:
    led = getattr(rt, "net", None)
    trace = TraceConfig()
    setattr(trace, _MARK, True)

    async def on_start(_session: Any, ctx: Any, _params: Any) -> None:
        ctx.start = time.monotonic()

    async def on_end(_session: Any, ctx: Any, params: Any) -> None:
        if led is None:
            return
        dur = int((time.monotonic() - getattr(ctx, "start", time.monotonic())) * 1000)
        url = params.response.url
        with contextlib.suppress(Exception):
            led.record(NetCall(
                ts=time.time(), subsystem=subsystem, method=params.method,
                host=url.host or "", path=url.path, status=params.response.status,
                duration_ms=dur))

    async def on_error(_session: Any, ctx: Any, params: Any) -> None:
        if led is None:
            return
        dur = int((time.monotonic() - getattr(ctx, "start", time.monotonic())) * 1000)
        url = params.url
        with contextlib.suppress(Exception):
            led.record(NetCall(
                ts=time.time(), subsystem=subsystem, method=params.method,
                host=url.host or "", path=url.path,
                duration_ms=dur, error=type(params.exception).__name__))

    trace.on_request_start.append(on_start)
    trace.on_request_end.append(on_end)
    trace.on_request_exception.append(on_error)
    return trace


class LedgerAiohttpSession(AiohttpSession):
    """An aiogram session that installs the ledger trace on the aiohttp
    ClientSession it lazily creates. aiohttp reads ``_trace_configs`` fresh on
    every request and requires each to be frozen, so appending a self-frozen
    config after creation is picked up from the next request on."""

    def __init__(self, rt: Any, *, subsystem: str = "telegram", **kw: Any) -> None:
        super().__init__(**kw)
        self._led_rt = rt
        self._led_subsystem = subsystem

    async def create_session(self) -> Any:
        session = await super().create_session()
        if not any(getattr(t, _MARK, False) for t in session._trace_configs):
            trace = make_trace_config(self._led_rt, self._led_subsystem)
            trace.freeze()
            session._trace_configs.append(trace)
        return session
