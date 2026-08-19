"""Gmail client: send + read, ported from jobpipe's GmailSender/GmailReader.

Kept synchronous (google-api-python-client is sync); async callers use
asyncio.to_thread. The `_build_service` seam is preserved so tests can fake
the Google service object without touching the network.
"""

from __future__ import annotations

import base64
import mimetypes
import random
import re
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..google_auth import GoogleAuth

_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GmailError(Exception):
    pass


class DailyCapExceeded(GmailError):
    """Consumer Gmail cap (~500 recipients/day): non-retryable until reset."""


def classify_http_error(exc: HttpError) -> str:
    """'daily_cap' | 'retry' | 'fatal' — the whole send policy in one place."""
    status = exc.resp.status if exc.resp is not None else 0
    body = (exc.content or b"").decode("utf-8", errors="replace").lower()
    if status == 403 and ("dailylimitexceeded" in body or "quota" in body):
        return "daily_cap"
    if status in (429, 500, 502, 503, 504):
        return "retry"
    return "fatal"


def build_message(
    *, to: str, subject: str, body: str,
    attachment_path: str | None = None, from_addr: str | None = None,
    in_reply_to: str | None = None, references: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if from_addr:
        msg["From"] = from_addr
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    if attachment_path:
        path = Path(attachment_path)
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype,
                           subtype=subtype, filename=path.name)
    return msg


class GmailClient:
    def __init__(self, auth: GoogleAuth) -> None:
        self._auth = auth

    def _build_service(self) -> Any:  # test seam — the only place Google is touched
        return build("gmail", "v1", credentials=self._auth.credentials(),
                     cache_discovery=False)

    # --- sending -------------------------------------------------------------

    def send(self, *, to: str, subject: str, body: str,
             attachment_path: str | None = None,
             thread_id: str | None = None,
             in_reply_to: str | None = None) -> str:
        if not _ADDRESS_RE.match(to):
            raise GmailError(f"invalid recipient address: {to!r}")
        msg = build_message(to=to, subject=subject, body=body,
                            attachment_path=attachment_path,
                            in_reply_to=in_reply_to)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        last: Exception | None = None
        for attempt in range(3):
            try:
                sent = (self._build_service().users().messages()
                        .send(userId="me", body=payload).execute())
                return str(sent["id"])
            except HttpError as exc:
                kind = classify_http_error(exc)
                if kind == "daily_cap":
                    raise DailyCapExceeded("Gmail daily send cap reached") from exc
                if kind == "fatal":
                    raise GmailError(f"Gmail send failed (HTTP {exc.resp.status})") from exc
                last = exc
                time.sleep((2 ** attempt) + random.uniform(0, 1))
        raise GmailError("Gmail send failed after retries") from last

    # --- reading -------------------------------------------------------------

    def list_messages(self, *, query: str, limit: int = 50) -> list[dict[str, str]]:
        service = self._build_service()
        out: list[dict[str, str]] = []
        token: str | None = None
        while len(out) < limit:
            resp = (service.users().messages()
                    .list(userId="me", q=query, maxResults=min(100, limit - len(out)),
                          pageToken=token).execute())
            out.extend(resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return out[:limit]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return (self._build_service().users().messages()
                .get(userId="me", id=message_id, format="full").execute())

    def mark_read(self, message_id: str) -> None:
        (self._build_service().users().messages()
         .modify(userId="me", id=message_id,
                 body={"removeLabelIds": ["UNREAD"]}).execute())

    def get_profile_address(self) -> str:
        profile = self._build_service().users().getProfile(userId="me").execute()
        return str(profile.get("emailAddress", ""))


# --- pure message-parsing helpers (from jobpipe supervisor/replies.py) --------

def header(message: dict[str, Any], name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def body_text(message: dict[str, Any]) -> str:
    """Walk nested parts, prefer text/plain."""

    def walk(part: dict[str, Any]) -> list[str]:
        texts: list[str] = []
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and (mime.startswith("text/plain") or mime.startswith("message/")):
            texts.append(_decode(data))
        for sub in part.get("parts", []) or []:
            texts.extend(walk(sub))
        return texts

    payload = message.get("payload", {})
    texts = walk(payload)
    if not texts:
        data = payload.get("body", {}).get("data")
        if data:
            texts.append(_decode(data))
    return "\n".join(t for t in texts if t.strip())


def sender_address(message: dict[str, Any]) -> str:
    raw = header(message, "From")
    match = re.search(r"<([^>]+)>", raw)
    return (match.group(1) if match else raw).strip().lower()


def sender_name(message: dict[str, Any]) -> str:
    raw = header(message, "From")
    name = re.sub(r"<[^>]+>", "", raw).strip().strip('"')
    return name or sender_address(message)
