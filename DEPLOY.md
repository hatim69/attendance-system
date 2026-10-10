# Deploying to a VPS

You need: a small VPS (Ubuntu 24.04, 1 GB RAM is plenty) and a domain name (or subdomain) you control.

## 1. Server
1. Create the VPS (DigitalOcean / Hetzner / Lightsail / etc.). Note its public IP.
2. In your domain's DNS add an **A record**: `attendance.yourdomain.com` -> the VPS IP. Wait a few minutes.
3. In the provider's firewall allow inbound **TCP 22, 80, 443**.
4. SSH in and install Docker:
   ```
   curl -fsSL https://get.docker.com | sh
   ```

## 2. App
```
git clone https://github.com/hatim69/attendance-system.git
cd attendance-system
git checkout main        # or the branch you want to deploy
cp .env.example .env
nano .env                # set DOMAIN, PUBLIC_DOMAIN, ADMIN_PASSWORD, SECRET_KEY (openssl rand -hex 32), TIMEZONE
docker compose up -d --build
docker compose logs -f   # Ctrl+C to leave; look for "FkWeb listener on port 80"
```
Open `https://attendance.yourdomain.com/` and log in as `admin` with your ADMIN_PASSWORD (used only the first time to create the account; afterwards use the dashboard's Password link).
Employees open `https://attendance.yourdomain.com/m/` on their phone and use "Add to Home Screen".

## 3. Point the fingerprint device at the server
On the device (Menu -> Comm): set **Webserver URL** to `http://attendance.yourdomain.com` (plain http, port 80), keep mode FkWeb, save, restart the device.
Scans then appear in the dashboard within seconds. Set `FKWEB_DEVICES` in `.env` to the device's id (`dev_id` in `data/fkweb_raw.log`) so only your device is accepted, then `docker compose up -d`.

## 4. First-time setup in the dashboard
0. **Users**: add logins for your team. Roles: *admin* (everything), *hr* (employees, shifts, leave, punches, reports, payroll, import) and *manager* (read-only, one department only, no salaries). Every action is enforced on the server and recorded in the Audit tab.
1. **Sites**: stand at the office, open the dashboard on your phone, tap "Use my location", name it, set the radius, Add.
2. **Employees**: add each person (ID = their ID on the device), then **Set PIN**.
3. Give employees their ID + PIN and the `/m/` link.

## Operations
- Update: `git pull && docker compose up -d --build`
- Backup (daily): copy the `data/` folder (database + selfies) somewhere off the server, e.g. `tar czf backup-$(date +%F).tgz data`
- Logs: `docker compose logs -f app`
- Security: the device port (80) is plain HTTP by device design. Use FKWEB_DEVICES, keep ADMIN_PASSWORD strong, and keep the server updated.
- Privacy: selfies and GPS are personal data. Tell employees what is collected and why, and delete old selfies per your policy.
