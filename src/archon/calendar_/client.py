"""Google Calendar v3 client (sync; async callers use asyncio.to_thread).

Ports the useful behaviors from calibot's src/google/calendar.ts: PATCH for
updates (not read-modify-PUT), RRULE support on create, and a clean error
type. Uses the shared combined-scope token."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..platforms.google_auth import GoogleAuth


class CalendarError(Exception):
    pass


def _wrap(exc: HttpError) -> CalendarError:
    status = exc.resp.status if exc.resp is not None else 0
    return CalendarError(f"Calendar API error (HTTP {status})")


class CalendarClient:
    def __init__(self, auth: GoogleAuth, timezone: str) -> None:
        self._auth = auth
        self.timezone = timezone

    def _svc(self) -> Any:  # test seam
        return build("calendar", "v3", credentials=self._auth.credentials(),
                     cache_discovery=False)

    def list_calendars(self) -> list[dict[str, Any]]:
        try:
            items = self._svc().calendarList().list().execute().get("items", [])
        except HttpError as exc:
            raise _wrap(exc) from exc
        return [{"id": c["id"], "summary": c.get("summary", ""),
                 "primary": c.get("primary", False)} for c in items]

    def create_event(
        self, *, calendar_id: str, title: str,
        start_iso: str, end_iso: str | None = None,
        all_day: bool = False, description: str | None = None,
        location: str | None = None, rrule: str | None = None,
        reminders_minutes: list[int] | None = None,
    ) -> dict[str, Any]:
        if all_day:
            start: dict[str, Any] = {"date": start_iso[:10]}
            end: dict[str, Any] = {"date": (end_iso or start_iso)[:10]}
        else:
            if not end_iso:
                end_dt = datetime.fromisoformat(start_iso) + timedelta(hours=1)
                end_iso = end_dt.isoformat()
            start = {"dateTime": start_iso, "timeZone": self.timezone}
            end = {"dateTime": end_iso, "timeZone": self.timezone}
        body: dict[str, Any] = {"summary": title, "start": start, "end": end}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if rrule:
            body["recurrence"] = [rrule if rrule.startswith("RRULE:") else f"RRULE:{rrule}"]
        if reminders_minutes:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": m} for m in reminders_minutes[:5]],
            }
        try:
            created = self._svc().events().insert(
                calendarId=calendar_id, body=body).execute()
        except HttpError as exc:
            raise _wrap(exc) from exc
        return {"id": created["id"], "htmlLink": created.get("htmlLink", ""),
                "summary": created.get("summary", "")}

    def list_events(self, *, calendar_id: str, time_min_iso: str,
                    time_max_iso: str, query: str | None = None,
                    limit: int = 25) -> list[dict[str, Any]]:
        try:
            resp = self._svc().events().list(
                calendarId=calendar_id, timeMin=time_min_iso, timeMax=time_max_iso,
                q=query, singleEvents=True, orderBy="startTime", maxResults=limit,
            ).execute()
        except HttpError as exc:
            raise _wrap(exc) from exc
        out = []
        for e in resp.get("items", []):
            out.append({
                "id": e["id"],
                "summary": e.get("summary", "(no title)"),
                "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
                "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
                "location": e.get("location"),
                "htmlLink": e.get("htmlLink"),
            })
        return out

    def update_event(self, *, calendar_id: str, event_id: str,
                     patch: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if "title" in patch:
            body["summary"] = patch["title"]
        if "description" in patch:
            body["description"] = patch["description"]
        if "location" in patch:
            body["location"] = patch["location"]
        if "start_iso" in patch:
            body["start"] = {"dateTime": patch["start_iso"], "timeZone": self.timezone}
        if "end_iso" in patch:
            body["end"] = {"dateTime": patch["end_iso"], "timeZone": self.timezone}
        try:
            updated = self._svc().events().patch(
                calendarId=calendar_id, eventId=event_id, body=body).execute()
        except HttpError as exc:
            raise _wrap(exc) from exc
        return {"id": updated["id"], "htmlLink": updated.get("htmlLink", "")}

    def delete_event(self, *, calendar_id: str, event_id: str) -> None:
        try:
            self._svc().events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            raise _wrap(exc) from exc

    def free_busy(self, *, calendar_id: str, time_min_iso: str,
                  time_max_iso: str) -> list[dict[str, str]]:
        try:
            resp = self._svc().freebusy().query(body={
                "timeMin": time_min_iso, "timeMax": time_max_iso,
                "items": [{"id": calendar_id}],
            }).execute()
        except HttpError as exc:
            raise _wrap(exc) from exc
        return resp.get("calendars", {}).get(calendar_id, {}).get("busy", [])
