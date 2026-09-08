"""Content-quality fixes: HTML-only email bodies, all-day event ranges, Google
error detail, and the owner-timezone clock."""

from __future__ import annotations

import base64
import json

import pytest

from archon.calendar_.client import CalendarClient, _reason
from archon.platforms.gmail.client import body_text


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_html_only_email_is_flattened_to_text():
    msg = {"payload": {"mimeType": "multipart/alternative", "body": {}, "parts": [
        {"mimeType": "text/html",
         "body": {"data": _b64("<html><body><p>Dentist <b>tomorrow</b> 15:00</p>"
                               "<script>x=1</script></body></html>")}},
    ]}}
    out = body_text(msg)
    assert "Dentist" in out and "tomorrow" in out and "15:00" in out
    assert "<b>" not in out and "script" not in out and "x=1" not in out


def test_plain_text_is_still_preferred_over_html():
    msg = {"payload": {"mimeType": "multipart/alternative", "body": {}, "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("plain wins")}},
        {"mimeType": "text/html", "body": {"data": _b64("<p>html loses</p>")}},
    ]}}
    assert body_text(msg) == "plain wins"


def test_single_part_html_message():
    msg = {"payload": {"mimeType": "text/html", "body": {"data": _b64("<p>hi there</p>")}}}
    assert body_text(msg).strip() == "hi there"


class _FakeEvents:
    def __init__(self, sink): self.sink = sink
    def insert(self, calendarId, body):
        self.sink["body"] = body
        return _Exec({"id": "e1", "htmlLink": "http://x"})


class _Exec:
    def __init__(self, r): self.r = r
    def execute(self): return self.r


class _FakeSvc:
    def __init__(self, sink): self._sink = sink
    def events(self): return _FakeEvents(self._sink)


def _client(sink):
    c = CalendarClient.__new__(CalendarClient)
    c.timezone = "Asia/Jerusalem"
    c._svc = lambda: _FakeSvc(sink)  # type: ignore[method-assign]
    return c


def test_all_day_event_with_no_end_gets_an_exclusive_next_day():
    sink: dict = {}
    _client(sink).create_event(calendar_id="primary", title="Holiday",
                               start_iso="2030-05-01", all_day=True)
    assert sink["body"]["start"] == {"date": "2030-05-01"}
    assert sink["body"]["end"] == {"date": "2030-05-02"}  # exclusive, non-empty


def test_all_day_event_equal_start_end_is_widened():
    sink: dict = {}
    _client(sink).create_event(calendar_id="primary", title="Day",
                               start_iso="2030-05-01", end_iso="2030-05-01", all_day=True)
    assert sink["body"]["end"] == {"date": "2030-05-02"}


def test_multi_day_all_day_range_is_preserved():
    sink: dict = {}
    _client(sink).create_event(calendar_id="primary", title="Trip",
                               start_iso="2030-05-01", end_iso="2030-05-04", all_day=True)
    assert sink["body"]["end"] == {"date": "2030-05-04"}


def test_google_error_reason_is_surfaced():
    class _Resp:
        status = 400

    class _HttpError(Exception):
        resp = _Resp()
        content = json.dumps({"error": {"message": "The specified time range is empty.",
                                        "errors": [{"reason": "timeRangeEmpty"}]}}).encode()

    assert "timeRangeEmpty" in _reason(_HttpError())
    assert "time range is empty" in _reason(_HttpError())


@pytest.mark.e2e
def test_current_datetime_uses_the_configured_timezone(tmp_path):
    import asyncio

    from archon.tools.registry import Registry, ToolContext
    from archon.tools import system as system_tools
    from test_m2 import make_rt

    rt = make_rt(tmp_path)
    rt.settings.timezone = "Asia/Jerusalem"
    reg = Registry(); system_tools.register(reg); rt.registry = reg
    out = json.loads(asyncio.run(reg.dispatch(ToolContext(rt=rt, scope="owner"),
                                              "current_datetime", {})))
    assert out["timezone"] == "Asia/Jerusalem"
    # The local time carries a +02:00/+03:00 offset, never the VM's UTC.
    assert out["local"] != out["utc"]
    assert "+0" in out["local"]
