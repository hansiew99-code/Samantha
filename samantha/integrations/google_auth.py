"""Google OAuth: one desktop-flow consent (scripts/setup_auth.py) covering
Calendar + Gmail + Google Chat; the refresh token persists at GOOGLE_TOKEN_PATH
and is auto-refreshed here on load.

The Chat scopes are read-only and only ever see spaces the owner is already a
member of (their DMs and rooms) — no bot install, no admin. If you authorised
before Chat was added, re-run scripts/setup_auth.py to widen the token; the
Chat integration stays dormant until GCHAT_ENABLED=1 regardless.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]


def load_credentials(token_path: Path):
    """Load stored credentials, refreshing if expired. Returns None when the
    token file is absent/invalid (Google integration disabled)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        return creds
    except Exception:
        log.exception("failed to load google credentials from %s", token_path)
        return None


def run_consent_flow(credentials_path: Path, token_path: Path, port: int = 0) -> None:
    """Interactive one-time consent (used by scripts/setup_auth.py).

    `port=0` picks a random local port (fine when a browser and this process
    share a machine). On a headless VPS, pass a fixed port and SSH-forward it
    (`ssh -L <port>:localhost:<port> ...`) so the browser redirect on your
    laptop reaches the server that's waiting here."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=port, open_browser=False)
    token_path.write_text(creds.to_json())
    print(f"Token saved to {token_path}")
