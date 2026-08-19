"""One-time Google OAuth consent (run on a machine with a browser).

Reuses the existing Desktop-app OAuth client from your-other-project
(client_secret.json is reusable across projects; jobpipe's own token file is
NOT touched). Produces ONE token with combined scopes:

    gmail.modify  +  calendar

Usage (from the repo root, with the venv):
    uv run python scripts/google_consent.py [path/to/client_secret.json]

Then copy secrets/google/token.json to the VM (deploy/MIGRATION.md).

Note: the OAuth consent screen must be "In production" (Google Cloud console
→ APIs & Services → OAuth consent screen), otherwise refresh tokens expire
every 7 days.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = REPO_ROOT / "secrets" / "google"
DEFAULT_JOBPIPE_SECRET = Path(
    r"C:\Users\YOURUSER\PycharmProjects\your-other-project\userdata\secrets\client_secret.json"
)


def main() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    client_secret = SECRETS_DIR / "client_secret.json"

    if not client_secret.exists():
        source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_JOBPIPE_SECRET
        if not source.exists():
            raise SystemExit(
                f"client_secret.json not found at {source}. Pass its path as an argument."
            )
        shutil.copy2(source, client_secret)
        print(f"Copied client secret from {source}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    token_path = SECRETS_DIR / "token.json"
    token_path.write_text(creds.to_json(), encoding="utf-8")
    granted = json.loads(creds.to_json()).get("scopes", [])
    print(f"\nToken written to {token_path}")
    print(f"Granted scopes: {granted}")
    if not creds.refresh_token:
        print("WARNING: no refresh_token returned — delete the token and re-run "
              "(make sure you see the consent screen, not a silent re-approval).")


if __name__ == "__main__":
    main()
