"""httpx clients that record every request to the network ledger.

`new_async_client(rt, subsystem=...)` is the drop-in replacement for the ad-hoc
``httpx.AsyncClient(...)`` calls across the codebase, and is what gets handed to
the Anthropic and OpenAI SDKs as ``http_client=`` so their traffic is observed
too. We override ``send`` rather than use event hooks because ``send`` is the
one place that sees BOTH the response and a raised connection error, so a failed
call (the interesting case — a dead host) is recorded, not silently missed.

google-genai accepts no ``http_client``, so Gemini's own API calls are not seen
here; they are covered by the socket probe and the VM packet tap. That gap is
intentional and documented, not an oversight.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import httpx

from ..netlog import NetCall


def _ledger(rt: Any) -> Any:
    return getattr(rt, "net", None)


def _content_len(headers: Any) -> int | None:
    try:
        n = int(headers.get("content-length") or 0)
    except (TypeError, ValueError):
        return None
    return n or None


class _LedgerAsyncClient(httpx.AsyncClient):
    def __init__(self, rt: Any, subsystem: str, purpose: str | None, **kw: Any) -> None:
        self._led = _ledger(rt)
        self._subsystem = subsystem
        self._purpose = purpose
        super().__init__(**kw)

    async def send(self, request: httpx.Request, **kw: Any) -> httpx.Response:
        start = time.monotonic()
        try:
            resp = await super().send(request, **kw)
        except Exception as exc:
            self._log(request, None, exc, start)
            raise
        self._log(request, resp, None, start)
        return resp

    def _log(self, request: httpx.Request, resp: httpx.Response | None,
             exc: Exception | None, start: float) -> None:
        if self._led is None:
            return
        with contextlib.suppress(Exception):
            self._led.record(NetCall(
                ts=time.time(), subsystem=self._subsystem, method=request.method,
                host=request.url.host, path=request.url.path,
                status=(resp.status_code if resp is not None else None),
                req_bytes=_content_len(request.headers),
                resp_bytes=(_content_len(resp.headers) if resp is not None else None),
                duration_ms=int((time.monotonic() - start) * 1000),
                error=(type(exc).__name__ if exc is not None else None),
                purpose=self._purpose,
            ))


class _LedgerSyncClient(httpx.Client):
    def __init__(self, rt: Any, subsystem: str, purpose: str | None, **kw: Any) -> None:
        self._led = _ledger(rt)
        self._subsystem = subsystem
        self._purpose = purpose
        super().__init__(**kw)

    def send(self, request: httpx.Request, **kw: Any) -> httpx.Response:
        start = time.monotonic()
        try:
            resp = super().send(request, **kw)
        except Exception as exc:
            self._log(request, None, exc, start)
            raise
        self._log(request, resp, None, start)
        return resp

    def _log(self, request: httpx.Request, resp: httpx.Response | None,
             exc: Exception | None, start: float) -> None:
        if self._led is None:
            return
        with contextlib.suppress(Exception):
            self._led.record(NetCall(
                ts=time.time(), subsystem=self._subsystem, method=request.method,
                host=request.url.host, path=request.url.path,
                status=(resp.status_code if resp is not None else None),
                req_bytes=_content_len(request.headers),
                resp_bytes=(_content_len(resp.headers) if resp is not None else None),
                duration_ms=int((time.monotonic() - start) * 1000),
                error=(type(exc).__name__ if exc is not None else None),
                purpose=self._purpose,
            ))


def new_async_client(rt: Any, *, subsystem: str, purpose: str | None = None,
                     timeout: float = 30.0, **kw: Any) -> httpx.AsyncClient:
    kw.setdefault("timeout", timeout)
    return _LedgerAsyncClient(rt, subsystem, purpose, **kw)


def new_sync_client(rt: Any, *, subsystem: str, purpose: str | None = None,
                    timeout: float = 30.0, **kw: Any) -> httpx.Client:
    kw.setdefault("timeout", timeout)
    return _LedgerSyncClient(rt, subsystem, purpose, **kw)
