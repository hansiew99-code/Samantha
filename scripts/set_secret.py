#!/usr/bin/env python3
"""Safely set one credential in .env — no shell history, no paste-corruption.

Usage (run from the samantha folder on the server):

    .venv/bin/python scripts/set_secret.py SLACK_BOT_TOKEN

Paste the value at the hidden prompt. If a browser terminal mangles the raw
value into bullet characters (the `sk-ant-…` trap), encode it on a trusted
LOCAL terminal first:

    python3 -c 'import base64,getpass;print(base64.b64encode(getpass.getpass("value: ").encode()).decode())'

copy the single base64 line it prints, and run here with --base64:

    .venv/bin/python scripts/set_secret.py SLACK_BOT_TOKEN --base64

The helper validates the value is clean ASCII (catching corruption), then
atomically rewrites .env with mode 0600, preserving every other line.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import tempfile
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Set one credential in .env safely.")
    ap.add_argument("var", help="e.g. SLACK_BOT_TOKEN")
    ap.add_argument("--base64", action="store_true",
                    help="the pasted value is base64-encoded (use if a web terminal corrupts the raw value)")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    entered = getpass.getpass(f"{args.var} (hidden): ").strip()
    if args.base64:
        try:
            entered = base64.b64decode(entered, validate=True).decode("ascii").strip()
        except Exception:
            raise SystemExit("Not valid base64/ASCII — nothing changed.")

    if not entered:
        raise SystemExit("Empty value — nothing changed.")
    if not entered.isascii():
        raise SystemExit(
            "Value contains non-text characters — it was corrupted on paste. "
            "Nothing changed. Re-run with --base64 (see this script's header)."
        )

    env_path = Path(args.env)
    lines = []
    if env_path.exists():
        lines = [
            line for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{args.var}=")
        ]
    lines.append(f"{args.var}={entered}")

    parent = env_path.parent if str(env_path.parent) else Path(".")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".env.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, env_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    print(f"{args.var} set ✓  ({len(entered)} chars). Restart to apply:  sudo systemctl restart samantha")


if __name__ == "__main__":
    main()
