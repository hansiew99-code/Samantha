# Deploying Samantha on a free VPS (Oracle Cloud Always Free)

Any always-on Linux host works; Oracle's Always Free tier is genuinely free
and more than enough for Samantha's footprint (~100 MB RAM).

## 1. Create the instance

1. Sign up at cloud.oracle.com (needs a credit card for identity, not billed).
2. Compute → Instances → Create. Image: **Ubuntu 24.04**. Shape: any Always
   Free-eligible shape (VM.Standard.E2.1.Micro, or an Ampere A1 with 1 OCPU /
   6 GB — also free).
3. Add your SSH public key. No inbound ports need opening — every Samantha
   transport is outbound (Telegram long-polling, Slack Socket Mode, Google/
   ClickUp HTTPS polling).

## 2. Install

```bash
ssh ubuntu@<instance-ip>
sudo apt update && sudo apt install -y python3-venv git
sudo mkdir -p /opt/samantha && sudo chown $USER /opt/samantha
git clone <repo-url> /opt/samantha && cd /opt/samantha
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env && nano .env       # fill in keys per README
```

## 3. Dedicated user + systemd

```bash
sudo useradd -r -s /usr/sbin/nologin samantha
sudo chown -R samantha:samantha /opt/samantha
sudo cp deploy/samantha.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now samantha
journalctl -u samantha -f               # watch her come up
```

`Restart=always` + reminder rehydration means power cycles and deploys are
safe: pending reminders re-schedule on boot, overdue ones fire immediately
with an "(overdue)" note.

## 4. Updating

```bash
cd /opt/samantha && sudo -u samantha git pull
sudo -u samantha .venv/bin/pip install -e .
sudo systemctl restart samantha
```

## 5. Backups

Everything Samantha knows lives in one SQLite file (`samantha.db`, WAL mode).
Nightly copy is plenty:

```bash
sudo crontab -u samantha -e
# 0 4 * * * sqlite3 /opt/samantha/samantha.db ".backup /opt/samantha/backups/samantha-$(date +\%a).db"
```

(Seven rotating daily backups; restore = stop service, copy file back, start.)
