#!/usr/bin/env python3
"""One-time Google OAuth consent (Calendar + Gmail + Chat).

Two files are involved and they are NOT the same thing:
  • google_credentials.json  — the OAuth *client* JSON you download from Google
                               Cloud Console. Identifies the app.
  • google_token.json        — YOUR login, produced by this script. Samantha
                               reads this on boot.

Prereqs (Google Cloud Console, one time):
  1. Create a project; enable the Calendar, Gmail, and (for Chat) Chat APIs.
  2. OAuth consent: Internal for an eligible Workspace, or External for a
     personal account. Do not leave External in Testing for continuous use:
     these scopes can otherwise produce seven-day refresh tokens. Move the app
     to In production before relying on it, then consent as yourself.
  3. Create an OAuth client id of type "Desktop app" → Download JSON → save it
     next to this repo as google_credentials.json.

Run it:
  • Same machine as your browser (e.g. a laptop):
        .venv/bin/python scripts/setup_auth.py
  • Headless VPS over SSH — forward a fixed port so the browser redirect on
    your laptop reaches this box, then run with that port:
        ssh -L 8765:localhost:8765 you@your-vps
        .venv/bin/python scripts/setup_auth.py --port 8765
    Open the printed URL in your laptop browser, approve every permission, and
    the least-privilege token lands in google_token.json here with owner-only
    file permissions — nothing to copy afterwards.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samantha.config import load_settings  # noqa: E402
from samantha.integrations.google_auth import run_consent_flow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a Google OAuth token for Samantha.")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Fixed local port for the OAuth redirect. Use with `ssh -L "
        "<port>:localhost:<port>` on a headless VPS; omit on a machine with a browser.",
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.google_credentials_path.exists():
        sys.exit(
            f"Missing {settings.google_credentials_path} — this is the OAuth *client* "
            "JSON, not your login token.\n"
            "Get it: https://console.cloud.google.com/apis/credentials → under "
            "'OAuth 2.0 Client IDs' click the download icon on your Desktop client "
            "→ Download JSON.\n"
            f"Then save/copy it here as '{settings.google_credentials_path}' and rerun.\n"
            "On a headless VPS, base64 it across:\n"
            "  laptop:  base64 client_secret_XXX.json    # copy the output\n"
            f"  vps:     base64 -d > {settings.google_credentials_path} <<'B64'\n"
            "           <paste>\n"
            "           B64"
        )
    run_consent_flow(settings.google_credentials_path, settings.google_token_path, port=args.port)


if __name__ == "__main__":
    main()
