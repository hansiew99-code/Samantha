"""Google OAuth: one desktop-flow consent (scripts/setup_auth.py) covering
Calendar + Gmail + Google Chat; the refresh token persists at GOOGLE_TOKEN_PATH
and is auto-refreshed here on load.

The Chat scopes are read-only and only ever see spaces the owner is already a
member of (their DMs and rooms) — no bot install, no admin. If you authorised
before Chat was added, re-run scripts/setup_auth.py to widen the token; the
Chat integration stays dormant until GCHAT_ENABLED=1 regardless.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCOPES = [
    # Least privilege for the operations Samantha actually implements.  The
    # old `calendar` + `gmail.modify` pair granted broader access than needed.
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]

# Tokens minted by Samantha before v0.2 used these broader Calendar and Gmail
# grants.  They already cover every operation represented by the narrower
# scopes above, but a refresh token cannot be refreshed by asking Google for a
# different scope set.  Keep this compatibility knowledge at load time only:
# new consent flows must continue to request SCOPES.
LEGACY_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]

_CAPABILITY_SCOPES = {
    "calendar events": {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar",
    },
    "calendar availability": {
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar",
    },
    "Gmail reading": {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    },
    "Gmail sending": {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
    },
    "Chat space reading": {
        "https://www.googleapis.com/auth/chat.spaces.readonly",
    },
    "Chat message reading": {
        "https://www.googleapis.com/auth/chat.messages.readonly",
    },
}


def _recorded_scopes(token_info: dict[str, Any]) -> list[str]:
    """Return the scopes stored with an authorised-user token.

    Google credentials JSON has used both an array and a space-delimited
    string here.  Refuse absent or malformed scope metadata: silently assuming
    capabilities would defer the failure until a live provider call.
    """
    raw_scopes = token_info.get("scopes")
    if isinstance(raw_scopes, str):
        scopes = raw_scopes.split()
    elif isinstance(raw_scopes, list) and all(isinstance(scope, str) for scope in raw_scopes):
        scopes = raw_scopes
    else:
        raise ValueError("Google token has no valid recorded scopes")

    # Preserve file order for stable serialisation while avoiding duplicates.
    unique_scopes = list(dict.fromkeys(scope for scope in scopes if scope))
    if not unique_scopes:
        raise ValueError("Google token has no recorded scopes")
    return unique_scopes


def _scope_profile(scopes: list[str]) -> tuple[str, list[str]]:
    """Classify a current/legacy-compatible grant and list missing abilities."""
    granted = set(scopes)
    missing = [
        capability
        for capability, alternatives in _CAPABILITY_SCOPES.items()
        if granted.isdisjoint(alternatives)
    ]
    if missing:
        return "insufficient", missing
    if set(SCOPES).issubset(granted):
        return "current", []
    if set(LEGACY_SCOPES).issubset(granted):
        return "legacy-compatible", []
    # A token produced during a staged scope migration may legitimately mix
    # current grants with their known legacy equivalents.
    return "compatible", []


def load_credentials(token_path: Path):
    """Load stored credentials, refreshing if expired. Returns None when the
    token file is absent/invalid (Google integration disabled)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        return None
    try:
        # Restrict the secret before reading it, including tokens written by an
        # older deployment with a permissive process umask.
        token_path.chmod(0o600)
        token_info = json.loads(token_path.read_text())
        if not isinstance(token_info, dict):
            raise ValueError("Google token must contain a JSON object")
        granted_scopes = _recorded_scopes(token_info)
        profile, missing = _scope_profile(granted_scopes)
        if missing:
            log.error(
                "google credentials are missing required capabilities: %s",
                ", ".join(missing),
            )
            return None

        # Refresh with the exact grant recorded in the token.  Substituting
        # SCOPES here breaks valid legacy refresh tokens with `invalid_scope`.
        creds = Credentials.from_authorized_user_file(str(token_path), granted_scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            token_path.chmod(0o600)
        log.info("loaded google credentials (%s scope profile)", profile)
        return creds
    except Exception as exc:
        # Credential and provider exceptions can carry response details.  Log
        # only the failure class; never token JSON or refresh responses.
        log.error("failed to load google credentials (%s)", type(exc).__name__)
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
    token_path.chmod(0o600)
    print(f"Token saved to {token_path}")
