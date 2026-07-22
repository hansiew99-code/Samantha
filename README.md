# Samantha

A proactive, persistent-memory personal AI assistant — Samantha from *Her* by way of Andy from *The Devil Wears Prada*. Telegram is her conversation, notification, and approval surface; the other integrations are sources she watches and tools she can use.

This branch contains an assistant overhaul, but it is not proof that a live instance has been upgraded. Read **[PATCH_NOTES.md](PATCH_NOTES.md)** for the exact change set and deployment status, and **[docs/PROACTIVE_ASSISTANT_RESEARCH.md](docs/PROACTIVE_ASSISTANT_RESEARCH.md)** for the research and remaining roadmap.

Original design and architecture: **[BRIEF.md](BRIEF.md)**. Treat the patch notes as the current implementation/limitations record. In short:

- **Deterministic core, LLM at the edges** — reminders, event filtering, free/busy math, and budget accounting are plain code (zero tokens); Claude only interprets, decides, and composes.
- **Conditional follow-through** — “if Shyan hasn't emailed by EOD, remind me tomorrow” becomes a persistent Gmail watch, not a static reminder. Samantha checks immediately, closes the loop when matching mail arrives, and verifies Gmail again before alerting.
- **Bounded working context** — durable messages and facts stay in SQLite, while recent conversation, retrieved facts, and the running summary each have a prompt budget. Nightly Batch-API consolidation checkpoints history without intentionally skipping clipped rows.
- **Cheap by default** — Haiku handles the day-to-day, self-escalating to Sonnet/Opus when a task deserves it; a timezone-aware budget governor degrades at 70% and stops starting new model calls after the recorded daily spend reaches 100%. A single in-flight request can still cross the configured figure.
- **Act privately; approve external effects** — read-only checks and private reversible actions should happen without another permission question. Email/Slack drafts, new calendar invitations, and calendar deletion arrive in Telegram for one-tap approval.
- **Quiet by default** — unsolicited sweeps, calendar nudges, and digests respect configured quiet hours. Explicit reminders and conditional watches still fire because the owner asked for them.
- **Standing rules** — "stop reminding me about ClickUp" persists as a rule enforced in code forever.

## Setup

### 1. Requirements

- Python ≥ 3.11 on the existing Ubuntu x86_64 host. For Google Cloud's current Free Tier constraints and IPv4 cost trap, see [deploy/setup_vps.md](deploy/setup_vps.md).
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

**Google Calendar + Gmail + optional Google Chat**
1. In [Google Cloud Console](https://console.cloud.google.com): create a project, enable the *Google Calendar API*, *Gmail API*, and — if wanted — *Google Chat API*.
2. OAuth consent screen → Internal (eligible Workspace) or External (personal account). For External, move the app to **In production** before continuous use; Testing refresh tokens for these scopes can expire after seven days.
3. Credentials → Create credentials → OAuth client ID → *Desktop app* → download the JSON to `google_credentials.json`.
4. `python scripts/setup_auth.py` — open the printed URL, approve, done. The refresh token lands in `google_token.json`.
5. Set `GCHAT_SELF_ID=users/<id>` first, then `GCHAT_ENABLED=1` after consenting if Samantha should read Google Chat. The self ID is required so her own messages are never surfaced as incoming work.

New Google consent requests `calendar.events`, `calendar.freebusy`, `gmail.readonly`, `gmail.send`, and read-only Chat access. Existing Samantha tokens with the previous `calendar` and `gmail.modify` grants remain compatible and refresh with their original grants; re-consent is only needed when a token lacks a required capability. New, loaded, and refreshed token files are restricted to owner read/write (`0600`). Samantha never needs your Google password. Treat `google_credentials.json`, `google_token.json`, `.env`, and the SQLite database as secrets; do not paste them into a chat, issue, commit, or pull request.

**Slack**
1. [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch.
2. Socket Mode → enable → create an app-level token with `connections:write` → `SLACK_APP_TOKEN` (xapp-…).
3. OAuth & Permissions → bot scopes: `app_mentions:read`, `im:history`, `chat:write` → install to workspace → `SLACK_BOT_TOKEN` (xoxb-…).
4. Event Subscriptions → enable → subscribe to bot events `app_mention` and `message.im`.

**ClickUp** — profile → Apps → API token → `CLICKUP_API_TOKEN`; the team id is the number in your ClickUp URL → `CLICKUP_TEAM_ID`.

## What “proactive” means in this build

- Gmail is polled every five minutes, Google Chat every three minutes, and ClickUp every ten minutes when configured. Slack arrives through Socket Mode. Calendar conflict and upcoming-meeting scans run on the configurable proactive heartbeat (30 minutes by default).
- New source events are stored, filtered by standing rules, and triaged in batches with their subject, source time, receipt time, current time, and VIP status. Useful-but-not-urgent items roll into the next brief; genuinely time-sensitive items are bundled into one Telegram push per sweep.
- Conditional Gmail watches are persistent across restarts. They use incoming messages as a fast path and a live Gmail search as the correctness check at the deadline/notification time. A Gmail outage is reported as “couldn't verify,” never as “the email did not arrive.”
- Morning, afternoon, and evening briefs include source freshness. If Calendar, Gmail, Google Chat, or ClickUp could not be checked, Samantha is instructed to say so instead of implying complete coverage.

Polling is the present implementation, not the final architecture. Gmail push notifications, Workspace Events for Google Chat, and Calendar webhooks/incremental sync are documented as follow-up work in [the research note](docs/PROACTIVE_ASSISTANT_RESEARCH.md).

## Autonomy and approvals

| Action | Current boundary |
| --- | --- |
| Search/read connected sources | Do it immediately; no extra approval |
| Create a private reminder, watch, memory fact, standing rule, or attendee-free calendar event | Do it, then confirm |
| Send email or Slack message | Draft in Telegram; send only after approval |
| Create a calendar event with attendees | Draft in Telegram; create/send invitations only after approval |
| Edit an existing calendar event | Update privately if it has no attendees; otherwise require Telegram approval |
| Delete a calendar event | Require Telegram approval |

Email, Chat, task, calendar, attachment, and web content is explicitly treated as untrusted evidence, never as authority to change rules or invoke actions. Only the owner speaking through the authorized Telegram chat can authorize a new action or standing instruction.

## Access needed to deploy this branch

For code review and offline tests, no inbox, Chat, Telegram, or production secrets are required. Synthetic fixtures are enough.

For a real deployment, the operator needs:

1. The branch deployed to the always-on host and permission to restart/read logs for the Samantha service. If someone else deploys it for you, give them a short-lived, scoped deploy account rather than a root password.
2. `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` placed directly in the host's `.env` by you.
3. A one-time Google OAuth consent on your account for Gmail/calendar, plus read-only Chat scopes if Google Chat monitoring is enabled. Complete the browser consent flow yourself; do not share the resulting refresh token in chat.
4. Optional Slack and ClickUp credentials only if those sources are in scope.
5. A staging window or test account for live smoke tests before replacing the current bot.

Repository access alone cannot update the running assistant. Deployment access and user-run OAuth are separate, and this README does not claim either has happened.

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
3. **Conditional watch** — “if Shyan hasn't emailed by 5pm, remind me at 9 tomorrow” → one confirmation says Samantha checked and is watching. Send a matching email before the deadline: no fallback should fire. Repeat without sending: the Telegram fallback should arrive after a final Gmail verification.
4. **Memory** — "remember my sister's birthday is 12 October" → next day (after the 03:00 consolidation), ask "when's my sister's birthday?" obliquely.
5. **Calendar** — "what's on my calendar tomorrow?"; then "find 30 minutes for me and <colleague@email> on Thursday" (their free/busy must be visible to you).
6. **Approval gate** — "draft a reply to the last email from X" → draft appears with Send/Edit/Discard; tap Send; check your Gmail sent folder. Creating an event with attendees and deleting an event should use the same gate.
7. **Rules** — "stop reminding me about ClickUp" → `/help`-free check: ClickUp due-soon pings stop; "what are your standing rules?" lists it; restart — still enforced.
8. **Quiet hours and freshness** — set a short test quiet window and confirm unsolicited sweeps/digests stay silent while an explicit reminder still fires. Temporarily break a test integration and confirm the next brief names the unchecked source.
9. **Budget** — `/spend` after a few days: verify usage is recorded in the configured timezone and model tier drops after the threshold. To exercise deterministic mode, temporarily set `SAMANTHA_DAILY_BUDGET_USD=0.01`. Do not treat the setting as a prepaid provider-side spending limit; an in-flight API call can overshoot it.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest            # 226 passing offline tests; no network or live keys
.venv/bin/python -m samantha --check-config
.venv/bin/python -m samantha --dry-run  # boots with zero credentials
```

Token budget defaults to $0.177/day (≈ RM25/month at RM4.7/USD) — change with `SAMANTHA_DAILY_BUDGET_USD`.
