# Samantha

A proactive, persistent-memory personal AI assistant — Samantha from *Her* by way of Andy from *The Devil Wears Prada*. She lives in your Telegram, runs 24/7 on a free VPS, never forgets, and keeps her Claude token spend under a hard budget (~RM25/month).

Design and architecture: **[BRIEF.md](BRIEF.md)**. In short:

- **Deterministic core, LLM at the edges** — reminders, event filtering, free/busy math, and budget accounting are plain code (zero tokens); Claude only interprets, decides, and composes.
- **Never forgets** — all memory in SQLite; facts are superseded, never deleted; a nightly Batch-API job consolidates each day into structured facts. Per-call context stays bounded no matter how much she remembers, so cost can't balloon.
- **Cheap by default** — Haiku handles the day-to-day, self-escalating to Sonnet/Opus when a task deserves it; a budget governor degrades gracefully at 70%/100% of the daily cap.
- **Confirm outbound only** — she manages your calendar/reminders/tasks freely, but anything reaching another person (email, Slack) arrives in Telegram as a draft with a Send button.
- **Standing rules** — "stop reminding me about ClickUp" persists as a rule enforced in code forever.

## Setup

### 1. Requirements

- Python ≥ 3.11 on an always-on Linux box (Oracle Cloud Always Free works — see [deploy/setup_vps.md](deploy/setup_vps.md))
- An Anthropic API key (pay-as-you-go)
- A Telegram account

### 2. Install

```bash
git clone <this repo> /opt/samantha && cd /opt/samantha
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env
```

### 3. Day-one credentials (required)

**Anthropic** — create a key at platform.claude.com → `ANTHROPIC_API_KEY`.

**Telegram** — message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token to `TELEGRAM_BOT_TOKEN`. Then message [@userinfobot](https://t.me/userinfobot) to get your numeric id → `TELEGRAM_CHAT_ID`. Samantha ignores everyone but this id.

That's enough to run: `.venv/bin/python -m samantha`. Everything else can be added later — unconfigured integrations just log "disabled".

### 4. Optional integrations (add anytime)

**Google Calendar + Gmail**
1. In [Google Cloud Console](https://console.cloud.google.com): create a project, enable the *Google Calendar API* and *Gmail API*.
2. OAuth consent screen → External → add yourself as a test user.
3. Credentials → Create credentials → OAuth client ID → *Desktop app* → download the JSON to `google_credentials.json`.
4. `python scripts/setup_auth.py` — open the printed URL, approve, done. The refresh token lands in `google_token.json`.

**Slack**
1. [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch.
2. Socket Mode → enable → create an app-level token with `connections:write` → `SLACK_APP_TOKEN` (xapp-…).
3. OAuth & Permissions → bot scopes: `app_mentions:read`, `im:history`, `chat:write` → install to workspace → `SLACK_BOT_TOKEN` (xoxb-…).
4. Event Subscriptions → enable → subscribe to bot events `app_mention` and `message.im`.

**ClickUp** — profile → Apps → API token → `CLICKUP_API_TOKEN`; the team id is the number in your ClickUp URL → `CLICKUP_TEAM_ID`.

### 5. Run as a service

```bash
sudo cp deploy/samantha.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now samantha
journalctl -u samantha -f
```

## Post-deploy smoke tests

Run these once she's live (they mirror the milestones in BRIEF §11):

1. **Echo of life** — message her anything; she replies in character.
2. **Reminders** — "remind me to stretch in 2 minutes" → reminder arrives with Done/Snooze buttons; `sudo systemctl restart samantha` before it fires → it still arrives (rehydration).
3. **Memory** — "remember my sister's birthday is 12 October" → next day (after the 03:00 consolidation), ask "when's my sister's birthday?" obliquely.
4. **Calendar** — "what's on my calendar tomorrow?"; then "find 30 minutes for me and <colleague@email> on Thursday" (their free/busy must be visible to you).
5. **Approval gate** — "draft a reply to the last email from X" → draft appears with Send/Edit/Discard; tap Send; check your Gmail sent folder.
6. **Rules** — "stop reminding me about ClickUp" → `/help`-free check: ClickUp due-soon pings stop; "what are your standing rules?" lists it; restart — still enforced.
7. **Digest** — morning brief arrives at 07:30 (configure via `SAMANTHA_MORNING_DIGEST`).
8. **Budget** — `/spend` after a few days: cache reads > 0 on Sonnet calls, daily spend ≤ $0.18. To see the governor act, temporarily set `SAMANTHA_DAILY_BUDGET_USD=0.01` and restart.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest            # 67 offline tests — no network, no keys
.venv/bin/python -m samantha --check-config
.venv/bin/python -m samantha --dry-run  # boots with zero credentials
```

Token budget defaults to $0.177/day (≈ RM25/month at RM4.7/USD) — change with `SAMANTHA_DAILY_BUDGET_USD`.
