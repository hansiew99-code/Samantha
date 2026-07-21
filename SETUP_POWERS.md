# Turning on Samantha's powers

Each power is optional and independent. Do them **one at a time**, easiest
first. After each: `sudo systemctl restart samantha`, then test it from
Telegram. If anything's off, the logs tell you why: `journalctl -u samantha -n 40 --no-pager`.

**Adding any secret to the server uses the safe helper** (validates it, writes
`.env` atomically, no shell-history leak):

```bash
cd ~/samantha
.venv/bin/python scripts/set_secret.py VARNAME
```

Paste the value at the hidden prompt. If a value comes out as bullet dots (the
`sk-ant-…` corruption), encode it on your **local** computer first —
`python3 -c 'import base64,getpass;print(base64.b64encode(getpass.getpass("value: ").encode()).decode())'`
— and run the helper with `--base64`.

---

## 1. ClickUp (easiest — ~3 min)

**Get the token**
1. Open **https://app.clickup.com** → click your avatar (bottom-left) → **Settings**.
2. Left menu → **Apps** → under **API Token** click **Generate** → **Copy** (starts with `pk_`).

**Get the team id**
3. Look at your ClickUp browser URL: `https://app.clickup.com/`**`9008123456`**`/v/…` — that number is your team id.

**Put them on the server**
```bash
cd ~/samantha
.venv/bin/python scripts/set_secret.py CLICKUP_API_TOKEN
.venv/bin/python scripts/set_secret.py CLICKUP_TEAM_ID
sudo systemctl restart samantha
```

**Test:** Telegram → "what are my ClickUp tasks?"

---

## 2. Slack (~10 min)

**Create the app**
1. **https://api.slack.com/apps** → **Create New App** → **From scratch** → name it "Samantha", pick your workspace.

**App-level token (Socket Mode)**
2. Left menu → **Socket Mode** → toggle **Enable Socket Mode** on → when prompted, generate an **app-level token** with scope `connections:write` → **Copy** (`xapp-…`).

**Bot token + scopes**
3. Left menu → **OAuth & Permissions** → under **Bot Token Scopes** add: `app_mentions:read`, `im:history`, `chat:write`.
4. Scroll up → **Install to Workspace** → **Allow** → copy the **Bot User OAuth Token** (`xoxb-…`).

**Events**
5. Left menu → **Event Subscriptions** → toggle **Enable Events** on → expand **Subscribe to bot events** → **Add Bot User Event** → add `app_mention` and `message.im` → **Save Changes**.

**Put them on the server**
```bash
cd ~/samantha
.venv/bin/python scripts/set_secret.py SLACK_APP_TOKEN
.venv/bin/python scripts/set_secret.py SLACK_BOT_TOKEN
sudo systemctl restart samantha
```

**Test:** in Slack, DM your bot or @mention it in a channel it's in; then Telegram → "any Slack messages I should see?"

---

## 3. Google Calendar + Gmail (most involved — OAuth, ~15 min)

This one needs a browser for the login step, so part of it happens on your
**laptop**. Three parts.

### Part A — create the credentials (in your browser)
1. Create a project: **https://console.cloud.google.com/projectcreate** → name "samantha" → **Create** (then make sure it's selected in the top bar).
2. Enable Calendar API: **https://console.cloud.google.com/apis/library/calendar-json.googleapis.com** → **Enable**.
3. Enable Gmail API: **https://console.cloud.google.com/apis/library/gmail.googleapis.com** → **Enable**.
4. Consent screen: **https://console.cloud.google.com/apis/credentials/consent** → **External** → app name + your email → on **Test users**, **Add** your own Gmail address → save. (Leave it in "Testing" — no need to publish.)
5. Create the client: **https://console.cloud.google.com/apis/credentials** → **Create Credentials** → **OAuth client ID** → Application type **Desktop app** → **Create** → **Download JSON**.

### Part B — turn that JSON into a login token (on your laptop, needs a browser)
6. Put the downloaded file in a folder as `credentials.json`, open a terminal there, and run:
```bash
pip install google-auth-oauthlib
python3 - <<'PY'
from google_auth_oauthlib.flow import InstalledAppFlow
SCOPES = ["https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/gmail.modify"]
creds = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)
open("google_token.json", "w").write(creds.to_json())
print("wrote google_token.json")
PY
```
A browser tab opens → approve. You'll see **"Google hasn't verified this app"** → **Advanced** → **Go to samantha (unsafe)** → **Allow**. That's expected for a personal app. It writes `google_token.json`.

### Part C — copy the token to the server
7. On your laptop, print the token as base64 (safe to paste):
```bash
base64 google_token.json      # copy ALL the output
```
8. On the server, decode it into place:
```bash
base64 -d > ~/samantha/google_token.json <<'B64'
<paste all the base64 lines here>
B64
sudo systemctl restart samantha
```

**Test:** Telegram → "what's on my calendar tomorrow?" and "any new emails worth seeing?" and "find 30 minutes for me and alex@example.com on Thursday".

---

Stuck on any step? Send me the step number and what you see — I'll unstick it.
