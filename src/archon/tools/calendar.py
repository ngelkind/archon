"""Calendar tools. Owner scope acts directly; inbound scope routes event
creation through the confirmation gate."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..calendar_.client import CalendarClient
from ..db import repo
from ..pipeline import confirm
from ..runtime import Runtime
from .registry import Registry, ToolContext

_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "start_iso": {"type": "string",
                      "description": "Event start, ISO 8601 local time, e.g. 2026-08-21T15:00:00"},
        "end_iso": {"type": "string", "description": "Optional; defaults to start + 1h"},
        "all_day": {"type": "boolean"},
        "description": {"type": "string"},
        "location": {"type": "string"},
        "rrule": {"type": "string", "description": "Optional RRULE, e.g. FREQ=WEEKLY;COUNT=8"},
    },
    "required": ["title", "start_iso"],
}


def _client(rt: Runtime) -> CalendarClient:
    client = rt.clients.get("calendar")
    if client is None:
        from ..platforms.google_auth import GoogleAuth

        client = CalendarClient(GoogleAuth(rt.settings.google_token_path),
                                rt.settings.timezone)
        rt.clients["calendar"] = client
    return client  # type: ignore[return-value]


def _default_calendar(rt: Runtime) -> str:
    return str(repo.setting_get(rt.db, "calendar.default_id", "primary"))


async def _create_event_executor(rt: Runtime, payload: dict[str, Any]) -> str:
    client = _client(rt)
    created = await asyncio.to_thread(
        client.create_event,
        calendar_id=payload.get("calendar_id") or _default_calendar(rt),
        title=payload["title"],
        start_iso=payload["start_iso"],
        end_iso=payload.get("end_iso"),
        all_day=bool(payload.get("all_day")),
        description=payload.get("description"),
        location=payload.get("location"),
        rrule=payload.get("rrule"),
    )
    rt.db.execute(
        "INSERT INTO events_created (chat_pk, source_msg_id, gcal_event_id, calendar_id, "
        "title, start_ts, end_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (payload.get("chat_pk"), payload.get("source_msg_id"), created["id"],
         payload.get("calendar_id") or _default_calendar(rt), payload["title"],
         payload["start_iso"], payload.get("end_iso")),
    )
    return f"event '{payload['title']}' at {payload['start_iso']} — {created['htmlLink']}"


confirm.register_executor("event.create", _create_event_executor)


def _range(period: str, tz: str) -> tuple[str, str]:
    now = datetime.now(ZoneInfo(tz))
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "tomorrow":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        end = start + timedelta(days=1)
    elif period == "month":
        start, end = now, now + timedelta(days=31)
    else:  # week
        start, end = now, now + timedelta(days=7)
    return start.isoformat(), end.isoformat()


def register(registry: Registry) -> None:
    @registry.tool(
        "calendar_create_event",
        "Create a Google Calendar event. From inbound-triggered runs the event "
        "goes to the owner for one-tap confirmation; from the owner chat it is "
        "created immediately.",
        _EVENT_SCHEMA,
        scopes=("owner", "inbound"),
        sensitive=True,
    )
    async def calendar_create_event(ctx: ToolContext, **kwargs: Any) -> str:
        payload = {k: v for k, v in kwargs.items() if v not in (None, "")}
        payload["chat_pk"] = ctx.origin_chat_pk
        payload["source_msg_id"] = ctx.extras.get("source_msg_id")
        if ctx.scope == "inbound":
            action_id = await confirm.request_confirmation(
                ctx.rt, kind="event.create", payload=payload,
                description=f"{payload.get('title')} @ {payload.get('start_iso')}"
                            + (f" ({payload.get('location')})" if payload.get("location") else ""),
                chat_pk=ctx.origin_chat_pk,
            )
            return json.dumps({"status": "pending_owner_confirmation", "action_id": action_id})
        result = await _create_event_executor(ctx.rt, payload)
        return json.dumps({"status": "created", "detail": result})

    @registry.tool(
        "calendar_list_events",
        "List upcoming calendar events for a period.",
        {
            "type": "object",
            "properties": {"period": {"type": "string",
                                      "enum": ["today", "tomorrow", "week", "month"]}},
            "required": ["period"],
        },
        scopes=("owner", "inbound"),
    )
    async def calendar_list_events(ctx: ToolContext, period: str = "week") -> str:
        start, end = _range(period, ctx.rt.settings.timezone)
        events = await asyncio.to_thread(
            _client(ctx.rt).list_events,
            calendar_id=_default_calendar(ctx.rt), time_min_iso=start, time_max_iso=end,
        )
        return json.dumps(events, ensure_ascii=False)

    @registry.tool(
        "calendar_search_events",
        "Search calendar events by text over the next 90 days (and past 30).",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        scopes=("owner", "inbound"),
    )
    async def calendar_search_events(ctx: ToolContext, query: str) -> str:
        tz = ZoneInfo(ctx.rt.settings.timezone)
        now = datetime.now(tz)
        events = await asyncio.to_thread(
            _client(ctx.rt).list_events,
            calendar_id=_default_calendar(ctx.rt),
            time_min_iso=(now - timedelta(days=30)).isoformat(),
            time_max_iso=(now + timedelta(days=90)).isoformat(),
            query=query,
        )
        return json.dumps(events, ensure_ascii=False)

    @registry.tool(
        "calendar_update_event",
        "Update fields of an existing event (get the id from list/search).",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "title": {"type": "string"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["event_id"],
        },
        sensitive=True,
    )
    async def calendar_update_event(ctx: ToolContext, event_id: str, **patch: Any) -> str:
        result = await asyncio.to_thread(
            _client(ctx.rt).update_event,
            calendar_id=_default_calendar(ctx.rt), event_id=event_id,
            patch={k: v for k, v in patch.items() if v},
        )
        return json.dumps({"status": "updated", **result})

    @registry.tool(
        "calendar_delete_event",
        "Delete a calendar event by id.",
        {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
        sensitive=True,
    )
    async def calendar_delete_event(ctx: ToolContext, event_id: str) -> str:
        await asyncio.to_thread(
            _client(ctx.rt).delete_event,
            calendar_id=_default_calendar(ctx.rt), event_id=event_id,
        )
        ctx.rt.db.execute(
            "UPDATE events_created SET status = 'cancelled' WHERE gcal_event_id = ?",
            (event_id,),
        )
        return json.dumps({"status": "deleted", "event_id": event_id})

    @registry.tool(
        "calendar_check_conflicts",
        "Check whether a time range is free or busy.",
        {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
            },
            "required": ["start_iso", "end_iso"],
        },
        scopes=("owner", "inbound"),
    )
    async def calendar_check_conflicts(ctx: ToolContext, start_iso: str, end_iso: str) -> str:
        tz = ZoneInfo(ctx.rt.settings.timezone)

        def _aware(iso: str) -> str:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(UTC).isoformat()

        busy = await asyncio.to_thread(
            _client(ctx.rt).free_busy,
            calendar_id=_default_calendar(ctx.rt),
            time_min_iso=_aware(start_iso), time_max_iso=_aware(end_iso),
        )
        return json.dumps({"busy": busy, "free": not busy})

    @registry.tool(
        "calendar_list_calendars",
        "List available Google calendars and which is the default.",
    )
    async def calendar_list_calendars(ctx: ToolContext) -> str:
        cals = await asyncio.to_thread(_client(ctx.rt).list_calendars)
        return json.dumps({"default": _default_calendar(ctx.rt), "calendars": cals},
                          ensure_ascii=False)

    @registry.tool(
        "calendar_set_default",
        "Set the default calendar id used by all calendar tools.",
        {
            "type": "object",
            "properties": {"calendar_id": {"type": "string"}},
            "required": ["calendar_id"],
        },
        sensitive=True,
    )
    async def calendar_set_default(ctx: ToolContext, calendar_id: str) -> str:
        repo.setting_set(ctx.rt.db, "calendar.default_id", calendar_id)
        return json.dumps({"ok": True, "default": calendar_id})
