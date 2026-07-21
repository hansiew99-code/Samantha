#!/usr/bin/env python3
"""One-time Google OAuth consent. Run on the VPS:

    1. Create a Google Cloud project, enable the Calendar API and Gmail API,
       configure an OAuth consent screen (type: External, test user = you),
       and create an OAuth client id of type "Desktop app".
    2. Download the client JSON to google_credentials.json (or the path in
       GOOGLE_CREDENTIALS_PATH).
    3. Run:  python scripts/setup_auth.py
       Open the printed URL in your local browser, approve, and the refresh
       token lands in google_token.json. Samantha picks it up on next boot.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samantha.config import load_settings  # noqa: E402
from samantha.integrations.google_auth import run_consent_flow  # noqa: E402


def main() -> None:
    settings = load_settings()
    if not settings.google_credentials_path.exists():
        sys.exit(
            f"Missing {settings.google_credentials_path} — download the OAuth "
            "client JSON from Google Cloud Console first (see this file's docstring)."
        )
    run_consent_flow(settings.google_credentials_path, settings.google_token_path)


if __name__ == "__main__":
    main()
