# Deploying Samantha on Google Cloud's smallest Ubuntu VM

Samantha runs as one Python process with SQLite. She does not need a managed
database, load balancer, Kubernetes, Cloud Run, Cloud NAT, or an inbound web
port. This keeps the service suitable for an x86_64 `e2-micro` VM.

## Know what "free" means

As of July 2026, Google Cloud's Compute Engine Free Tier covers:

- one non-preemptible `e2-micro` worth of monthly runtime in `us-west1`,
  `us-central1`, or `us-east1`;
- 30 GB-months of standard persistent disk; and
- 1 GB/month of eligible outbound data transfer from North America.

Those allowances are account-wide and can change. Check the current
[Google Cloud Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features)
before deploying.

There are two important cost traps:

1. An always-on external IPv4 address is not covered beyond one hour per month.
   At the current list price of $0.005/hour it is about US$3.60/month. External
   IPv6 addresses are not charged, but an IPv6-only deployment works only while
   every configured provider endpoint is reachable over IPv6. A strict-$0 setup
   uses external IPv6, removes external IPv4, and administers the VM through IAP
   SSH. Test Telegram, Anthropic, Google, Slack, ClickUp, and Ubuntu package
   endpoints from the VM before removing IPv4. Do not add Cloud NAT to save the
   IPv4 charge; Cloud NAT has hourly, address, and traffic fees.
2. The Anthropic API is separate from Google Cloud and is not free. Samantha's
   daily model budget limits spend; it cannot make paid model calls free.

For the lowest practical bill, keep the existing VM only if it is an `e2-micro`
in one of the three eligible US regions, use a `pd-standard` boot disk no larger
than 30 GB, install no paid networking/observability products, and set a billing
budget alert. Google Cloud budget alerts are warnings, not a hard spending cap.

## 1. Prepare the Ubuntu x86_64 VM

Ubuntu 24.04 amd64 is suitable. Samantha makes outbound connections for Telegram
long polling, Slack Socket Mode, Google APIs, ClickUp, and Anthropic. She does
not require an inbound application port. Restrict SSH to your own IP, or use
[IAP TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding)
without assigning the VM a public IPv4 address.

On a 1 GB `e2-micro`, a small swap file prevents dependency installation from
being killed during a transient memory spike:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Do this only once; check first with `swapon --show` on an existing host.

## 2. Install Samantha

```bash
sudo apt update
sudo apt install -y python3-venv git sqlite3
sudo mkdir -p /opt/samantha
sudo chown "$USER" /opt/samantha
git clone https://github.com/hansiew99-code/Samantha.git /opt/samantha
cd /opt/samantha
python3 -m venv .venv
.venv/bin/pip install --upgrade -e .
cp .env.example .env
chmod 600 .env
```

Enter secrets directly in `/opt/samantha/.env`; never put them in GitHub, a
support ticket, or chat. Re-run `scripts/setup_auth.py` on the host after a
scope change so `google_token.json` also stays on the VM.

## 3. Run as a restricted systemd service

```bash
sudo useradd -r -s /usr/sbin/nologin samantha
sudo chown -R samantha:samantha /opt/samantha
sudo chmod 700 /opt/samantha
sudo chmod 600 /opt/samantha/.env
sudo cp deploy/samantha.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now samantha
sudo journalctl -u samantha -f
```

`Restart=always`, reminder rehydration, and durable event queues make service
restarts recoverable. Keep logs in systemd's journal instead of installing a
paid logging stack; set a modest journal cap in `/etc/systemd/journald.conf` if
disk use becomes material.

## 4. Update safely

```bash
cd /opt/samantha
sudo -u samantha git pull --ff-only
sudo -u samantha .venv/bin/pip install --upgrade -e .
sudo systemctl restart samantha
sudo systemctl status samantha --no-pager
```

Back up `samantha.db` before an upgrade. Do not overwrite `.env`, OAuth files,
or the database from the repository.

## 5. Keep local rotating backups

Everything Samantha knows lives in one SQLite database in WAL mode. Keeping
seven small local backups avoids adding a paid storage service:

```bash
sudo install -d -o samantha -g samantha -m 700 /opt/samantha/backups
sudo crontab -u samantha -e
# 0 4 * * * sqlite3 /opt/samantha/samantha.db ".backup /opt/samantha/backups/samantha-$(date +\%a).db"
```

Periodically verify a backup can be opened. A local backup protects against a
bad deployment, but not loss of the VM or disk; add encrypted off-host backup
only if that risk justifies the storage/transfer cost.

## 6. Verify the bill, not just the configuration

In Google Cloud Billing, confirm each month that the VM runtime and standard
disk are fully offset by Free Tier credits, outbound transfer stays under the
allowance, and no Cloud NAT, load balancer, static unused address, premium disk,
GPU, Ops Agent export, or non-eligible region has appeared. See current
[VPC network pricing](https://cloud.google.com/vpc/network-pricing) for address
and transfer charges.
