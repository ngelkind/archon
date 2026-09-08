"""httplib2 hook so Gmail/Calendar API calls land in the ledger.

google-api-python-client talks over httplib2, not httpx, so the httpx factory
can't see it. We build the service with an ``AuthorizedHttp`` whose inner
``httplib2.Http`` is subclassed to record each request. Google API traffic is
always to ``*.googleapis.com`` (an expected host), so this is about VISIBILITY
— proving Gmail/Calendar actually did something — not the egress tripwire.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any
from urllib.parse import urlsplit

import httplib2

from ..netlog import NetCall


class _LedgerHttp(httplib2.Http):
    def __init__(self, rt: Any, subsystem: str, purpose: str | None) -> None:
        self._led = getattr(rt, "net", None)
        self._subsystem = subsystem
        self._purpose = purpose
        super().__init__()

    def request(self, uri: str, method: str = "GET", body: Any = None,
                headers: Any = None, **kw: Any) -> Any:
        start = time.monotonic()
        try:
            resp, content = super().request(uri, method, body, headers, **kw)
        except Exception as exc:
            self._log(uri, method, None, None, exc, start)
            raise
        self._log(uri, method, getattr(resp, "status", None),
                  len(content) if content else None, None, start)
        return resp, content

    def _log(self, uri: str, method: str, status: int | None,
             resp_bytes: int | None, exc: Exception | None, start: float) -> None:
        if self._led is None:
            return
        u = urlsplit(uri)
        with contextlib.suppress(Exception):
            self._led.record(NetCall(
                ts=time.time(), subsystem=self._subsystem, method=method,
                host=u.hostname or "", path=u.path, status=status,
                resp_bytes=resp_bytes,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=(type(exc).__name__ if exc is not None else None),
                purpose=self._purpose))


def authorized_http(rt: Any, credentials: Any, *, subsystem: str = "google",
                    purpose: str | None = None) -> Any:
    """An ``AuthorizedHttp`` (credentials + ledger-recording transport) to hand
    to ``googleapiclient.discovery.build(http=...)``."""
    import google_auth_httplib2

    return google_auth_httplib2.AuthorizedHttp(
        credentials, http=_LedgerHttp(rt, subsystem, purpose))
