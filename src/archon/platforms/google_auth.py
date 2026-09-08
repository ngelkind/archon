"""Shared Google credentials (Gmail + Calendar use one combined-scope token).

Two backings, one refresh path:

* **File** — the single-user owner. The token is created interactively by
  scripts/google_consent.py on a desktop machine and copied to the VM; here we
  only load and refresh it. Unchanged behaviour for the live deployment.
* **Tenant** — the multi-user product. The token lives in
  ``integration_credentials``, encrypted per tenant (see ``crypto.py``), and is
  written back re-encrypted after a refresh.

Both go through :class:`GoogleAuth`, so token refresh, error handling and
scope checks exist once. Refresh failures surface as GoogleAuthError so the
owner (or the tenant) can be prompted to re-consent.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# TODO(TOS-REVIEW): Google — requests restricted Gmail scopes subject to Google's CASA security assessment and Limited Use — review before launch
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


class GoogleAuthError(Exception):
    pass


class CredentialStore(Protocol):
    """Where an authorized-user token JSON lives."""

    def load(self) -> dict[str, Any] | None: ...

    def save(self, data: dict[str, Any]) -> None: ...

    def describe(self) -> str:
        """Human-readable location, for error messages."""


class FileCredentialStore:
    """The owner's token.json on disk (single-user path)."""

    def __init__(self, token_path: Path) -> None:
        self._path = Path(token_path)

    def load(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        return json.loads(self._path.read_text(encoding="utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data), encoding="utf-8")

    def describe(self) -> str:
        return f"{self._path} (run scripts/google_consent.py and copy it over)"


class TenantCredentialStore:
    """One tenant's Google token, encrypted at rest in the database.

    The envelope's associated data binds it to this tenant, so a row moved
    between tenants fails to decrypt instead of yielding a working credential.
    """

    PROVIDER = "google"

    def __init__(self, rt: Any, tenant_id: int) -> None:
        self._rt = rt
        self._tenant_id = tenant_id

    def _scope(self):
        from ..db.tenancy import TenantScope

        return TenantScope(self._rt.db, self._tenant_id)

    def load(self) -> dict[str, Any] | None:
        from ..crypto import decrypt
        from ..db import repo

        row = repo.integration_cred_get(self._scope(), self.PROVIDER)
        if row is None:
            return None
        plaintext = decrypt(
            self._rt.settings.credential_encryption_key, row["secret_envelope"],
            tenant_id=self._tenant_id, purpose=self.PROVIDER,
        )
        return json.loads(plaintext)

    def save(self, data: dict[str, Any]) -> None:
        from ..crypto import encrypt
        from ..db import repo

        envelope = encrypt(
            self._rt.settings.credential_encryption_key,
            json.dumps(data), tenant_id=self._tenant_id, purpose=self.PROVIDER,
        )
        repo.integration_cred_upsert(
            self._scope(), provider=self.PROVIDER, secret_envelope=envelope,
            account_label=data.get("account_label"),
            scopes=" ".join(data.get("scopes") or SCOPES),
        )

    def describe(self) -> str:
        return f"tenant {self._tenant_id}'s linked Google account (re-link to fix)"


class GoogleAuth:
    def __init__(self, source: Path | str | CredentialStore) -> None:
        # Path/str keeps every existing single-user call site working unchanged.
        self._store: CredentialStore = (
            FileCredentialStore(Path(source))
            if isinstance(source, (str, Path)) else source
        )
        self._lock = threading.Lock()
        self._creds: Credentials | None = None

    def credentials(self) -> Credentials:
        with self._lock:
            if self._creds is None:
                data = self._store.load()
                if not data:
                    raise GoogleAuthError(
                        f"Google account not linked: {self._store.describe()}"
                    )
                self._creds = Credentials.from_authorized_user_info(data, SCOPES)
            if self._creds.expired and self._creds.refresh_token:
                try:
                    self._creds.refresh(Request())
                except RefreshError as exc:
                    raise GoogleAuthError(
                        "Google token refresh failed (revoked or expired) — "
                        f"re-consent needed: {self._store.describe()}"
                    ) from exc
                self._store.save(json.loads(self._creds.to_json()))
            return self._creds
