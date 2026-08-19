"""Shared Google credentials (Gmail + Calendar use one combined-scope token).

Headless-friendly: the token is created interactively by
scripts/google_consent.py on a desktop machine and copied to the VM; here we
only load and refresh it. Refresh failures surface as GoogleAuthError so the
owner can be alerted to re-consent.
"""

from __future__ import annotations

import threading
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


class GoogleAuthError(Exception):
    pass


class GoogleAuth:
    def __init__(self, token_path: Path) -> None:
        self._token_path = token_path
        self._lock = threading.Lock()
        self._creds: Credentials | None = None

    def credentials(self) -> Credentials:
        with self._lock:
            if self._creds is None:
                if not self._token_path.exists():
                    raise GoogleAuthError(
                        f"Google token not found at {self._token_path}; "
                        "run scripts/google_consent.py and copy it over."
                    )
                self._creds = Credentials.from_authorized_user_file(
                    str(self._token_path), SCOPES
                )
            if self._creds.expired and self._creds.refresh_token:
                try:
                    self._creds.refresh(Request())
                except RefreshError as exc:
                    raise GoogleAuthError(
                        "Google token refresh failed (revoked or expired) — "
                        "re-run scripts/google_consent.py"
                    ) from exc
                self._token_path.write_text(self._creds.to_json(), encoding="utf-8")
            return self._creds
