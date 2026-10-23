# Deploying to a VPS (Hostinger KVM, Ubuntu)

You need: the VPS (SSH as `root`, Hostinger's **Web console** button works too) and, for HTTPS, a hostname that points at it.

## 1. Pick the hostname (HTTPS needs one)
- **Quick start:** Hostinger gives every VPS a name like `srv1949824.hstgr.cloud` that already points at its IP. Use it as `DOMAIN` below.
- **Your own subdomain** (nicer for staff): in your domain's DNS add an **A record** `attendance` -> the VPS IPv4. Wait a few minutes, then use `attendance.yourdomain.com` as `DOMAIN`.
- The **fingerprint devices don't need a hostname**: they work with the plain IP (`http://<VPS IP>`).

## 2. Open the ports
Hostinger panel -> VPS -> **Firewall**: create a rule set allowing inbound **TCP 22, 80, 443** only and apply it to the VPS (with 0 rules everything is open).

## 3a. Fastest: one command (Web console or SSH as root)
```
curl -fsSL https://raw.githubusercontent.com/hatim69/attendance-system/claude/eager-wright-hkels3/deploy/install.sh | DOMAIN=srv1949824.hstgr.cloud bash
```
If the VPS already runs **nginx** (other websites), the installer detects it and shares the server: it adds ONE new nginx site file for this system, tests the whole nginx config before applying anything, reloads (not restarts) nginx, and never touches your other sites. Fingerprint devices must then use the hostname URL (`http://<DOMAIN>`), not the bare IP. To remove it again: `rm /etc/nginx/sites-enabled/attendance-system && nginx -t && systemctl reload nginx && cd /opt/attendance-system && docker compose -f docker-compose.nginx.yml down`.

It installs Docker, fetches the code, creates `.env` with a random admin password and secret key, starts everything and prints the login and the device URL. Safe to re-run (keeps your data and `.env`).

## 3b. Manual steps (what the installer does)
```
apt-get update && apt-get install -y docker.io docker-compose-v2 git
git clone https://github.com/hatim69/attendance-system.git
cd attendance-system
git checkout claude/eager-wright-hkels3     # or main, once the pull request is merged
cp .env.example .env
nano .env        # set DOMAIN, PUBLIC_DOMAIN, ADMIN_PASSWORD (long!), SECRET_KEY (openssl rand -hex 32), COMPANY_NAME, FKWEB_DEVICES
docker compose up -d --build
docker compose logs -f app     # Ctrl+C to leave; look for "FkWeb listener on port 80"
```
If `git clone` asks for a password the repository is private: create a read-only GitHub token (Settings -> Developer settings -> Fine-grained tokens -> this repo -> Contents: read) and use it as the password.

Open `https://<DOMAIN>/` and log in as `admin` with your ADMIN_PASSWORD (used only the first time, to create the account; afterwards use the dashboard's Password link). Employees open `https://<DOMAIN>/m/` on their phone and use "Add to Home Screen".

## 4. Point the fingerprint device at the server
On the device (Menu -> Comm): set **Webserver URL** to `http://<VPS IP>` or `http://<DOMAIN>` (plain http, port 80, no `https`), keep mode FkWeb, save, restart the device.
Scans then appear in the dashboard within seconds. Set `FKWEB_DEVICES` in `.env` to the device's id (`dev_id` in `data/fkweb_raw.log`) so only your device is accepted, then `docker compose up -d`.

## 5. First-time setup in the dashboard
0. **Users**: add logins for your team. Roles: *admin* and *management* (everything), *hr* (day-to-day work; you can remove or add individual abilities per person) and *team lead* (their own team only: change shifts and fix punch-in/out times, plus any extra abilities you tick for them). Every action is enforced on the server and recorded in the Audit tab.
0b. **Leave policy** (Leave tab and the phone app): starts 3 months after joining; Normal = 2 per month with 7 days' notice; Adhoc = 1 per 3 months for emergencies. Set each employee's **joining date** in Employees so balances are right.
1. **Sites**: stand at the office, open the dashboard on your phone, tap "Use my location", name it, set the radius, Add.
2. **Employees**: add each person (ID = their ID on the device), then **Set PIN**.
3. Give employees their ID + PIN and the `/m/` link.

## Operations
- Update: `git pull && docker compose up -d --build`
- Backups: automatic, every night at 2 AM (see **Nightly backup** below). The Backups page in the dashboard shows
  the last result and lets an admin download a copy or back up now. Selfie photos are not in the nightly file; to keep
  them too, copy `data/selfies` off the server now and then.
- Logs: `docker compose logs -f app`
- Security: the device port (80) is plain HTTP by device design. Use FKWEB_DEVICES, keep ADMIN_PASSWORD strong, and keep the server updated.
- Privacy: selfies and GPS are personal data. Tell employees what is collected and why, and delete old selfies per your policy.

## Nightly backup

Every night at 2 AM the server zips the whole database and the employee documents into
`data/backups/attendance-backup-YYYY-MM-DD.zip`, keeps the last 30 days, and emails the file to `BACKUP_EMAIL_TO`.

Add these lines to `/opt/attendance-system/.env` (`nano .env`), then run `docker compose -f docker-compose.nginx.yml up -d`:

```
BACKUP_EMAIL_TO=hr@madarwala.com
BACKUP_PASSWORD=a-long-password-only-HR-knows
SMTP_HOST=smtp.hostinger.com        # your mail provider's SMTP server (Google Workspace: smtp.gmail.com)
SMTP_PORT=465                        # 465 = SSL, 587 = STARTTLS
SMTP_USER=attendance@madarwala.com   # the mailbox the server sends from
SMTP_PASSWORD=that-mailbox's-password (Google Workspace: an "app password")
```

- The backup file is encrypted with `BACKUP_PASSWORD`. Open it with **7-Zip** or **WinRAR** (Windows' built-in zip
  cannot open encrypted files). Keep the password somewhere safe: without it the backup cannot be opened.
- Optional: `BACKUP_HOUR=2` (local time) and `BACKUP_KEEP_DAYS=30`.
- To check it works, open **Backups** in the dashboard and press **Back up now**.
- Restoring: stop the app, put `attendance.db` back as `data/attendance.db` (and `documents/` as `data/documents`),
  start the app. The README inside every backup says the same.

