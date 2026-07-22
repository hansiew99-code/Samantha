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
3b. *(only if you want Google Chat too)* Enable Chat API: **https://console.cloud.google.com/apis/library/chat.googleapis.com** → **Enable**.
4. Consent screen: **https://console.cloud.google.com/apis/credentials/consent**. For an eligible Google Workspace, use **Internal**. For a personal account, choose **External**, add yourself during setup, then move the app's publishing status to **In production** before relying on Samantha continuously. External apps left in **Testing** can receive seven-day refresh tokens for these non-basic scopes. A personal unverified app may still show Google's warning; only continue for the project you created yourself. If Google later reports `invalid_grant`, check publishing status and run the consent flow again.
5. Create the client: **https://console.cloud.google.com/apis/credentials** → **Create Credentials** → **OAuth client ID** → Application type **Desktop app** → **Create** → **Download JSON**.

### Part B — turn that JSON into a login token (on your laptop, needs a browser)
6. Put the downloaded file in a folder as `credentials.json`, open a terminal there, and run:
```bash
pip install google-auth-oauthlib
python3 - <<'PY'
import os
from google_auth_oauthlib.flow import InstalledAppFlow
SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.freebusy",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send",
          # keep the next two only if you enabled the Chat API in step 3b:
          "https://www.googleapis.com/auth/chat.spaces.readonly",
          "https://www.googleapis.com/auth/chat.messages.readonly"]
creds = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)
open("google_token.json", "w").write(creds.to_json())
os.chmod("google_token.json", 0o600)
print("wrote google_token.json")
PY
```
A browser tab opens → approve. You'll see **"Google hasn't verified this app"** → **Advanced** → **Go to samantha (unsafe)** → **Allow**. That's expected for a personal app. It writes `google_token.json`.

### Part C — copy the token to the server
7. On your laptop, encode the token as base64 for reliable transport. Base64
is **not encryption**: anyone who sees it can recover the OAuth token, so only
move it through a trusted private channel and clear it from clipboard/history:
```bash
base64 google_token.json      # copy ALL the output
```
8. On the server, decode it into place:
```bash
base64 -d > ~/samantha/google_token.json <<'B64'
<paste all the base64 lines here>
B64
chmod 600 ~/samantha/google_token.json
sudo systemctl restart samantha
```

**Test:** Telegram → "what's on my calendar tomorrow?" and "any new emails worth seeing?" and "find 30 minutes for me and alex@example.com on Thursday".

---

## 4. Google Chat (add-on to #3 — ~2 min)

Lets her see when people message you on Google Chat and flag the ones worth
your attention, the same way she watches Gmail. It rides on the Google login
you already did — you just need the two extra scopes and the API turned on. That
means re-minting `google_token.json` with the wider scopes.

**1. Enable the Chat API** (browser, "samantha" project selected):
https://console.cloud.google.com/apis/library/chat.googleapis.com → **Enable**.

**2. Make sure the client JSON is on the server.** Re-minting needs
`google_credentials.json` (the OAuth *client* file — not your token). If you
only ever copied the token before, put the client file on the VPS now:
- Download it: https://console.cloud.google.com/apis/credentials → the download
  icon on your **Desktop** OAuth client → Download JSON.
- Move it across (base64 avoids paste corruption):
```bash
# on your laptop:
base64 client_secret_XXX.json          # copy all output
# on the VPS:
base64 -d > ~/samantha/google_credentials.json <<'B64'
<paste>
B64
chmod 600 ~/samantha/google_credentials.json
```

**3. Re-mint the token with the Chat scopes.** The consent step needs a browser,
and the redirect comes back to `localhost` — so on a headless VPS, SSH-forward a
port first, then run the helper (it requests all six least-privilege scopes):
```bash
# reconnect to the VPS forwarding a port:
ssh -L 8765:localhost:8765 you@your-vps
cd ~/samantha
.venv/bin/python scripts/setup_auth.py --port 8765
```
Open the printed URL in your laptop browser, approve **all six** scopes
(two Calendar, two Gmail, and two read-only Chat scopes). The token is rewritten in place on
the VPS — nothing to copy.

**4. Set your own Chat user ID, then turn it on.** This is required so
Samantha never mistakes your own messages for incoming work:
Use the Google Chat API Explorer to list messages in a space where you have
posted, find one of your messages, and copy its `sender.name` value
(`users/<id>`).
```bash
.venv/bin/python scripts/set_secret.py GCHAT_SELF_ID       # enter: users/<id>
.venv/bin/python scripts/set_secret.py GCHAT_ENABLED      # enter: 1
sudo systemctl restart samantha
.venv/bin/python -m samantha --check-config                # expect: google chat: ok
```

**Test:** have someone message you on Google Chat, then Telegram → "any Google
Chat messages I should see?"

> Note: Chat is **read-only** here — she'll surface and summarise messages but
> won't reply to Chat on your behalf (Gmail and Slack are the ones she drafts
> replies for).

---

Stuck on any step? Send me the step number and what you see — I'll unstick it.
