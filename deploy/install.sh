#!/usr/bin/env bash
# One-shot installer for the attendance system on an Ubuntu/Debian VPS. Run as root (e.g. in Hostinger's Web console):
#   curl -fsSL https://raw.githubusercontent.com/hatim69/attendance-system/claude/eager-wright-hkels3/deploy/install.sh | DOMAIN=srv1949824.hstgr.cloud bash
# Re-running is safe: it updates the code and keeps your existing .env, database and selfies.
set -euo pipefail

REPO="${REPO:-https://github.com/hatim69/attendance-system.git}"
BRANCH="${BRANCH:-claude/eager-wright-hkels3}"
DIR="${DIR:-/opt/attendance-system}"
DOMAIN="${DOMAIN:-}"
COMPANY_NAME="${COMPANY_NAME:-Madarwala Tours and Travels}"
TIMEZONE="${TIMEZONE:-Asia/Kolkata}"
DEVICES="${DEVICES:-RSS202506120920,RSS202506120926,RSS202506120928}"   # Ranigunz, Qutbi Arcade, Mumbai Office
DRY_RUN="${DRY_RUN:-0}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
die() { echo "ERROR: $*" >&2; exit 1; }

[ "$DRY_RUN" = 1 ] || [ "$(id -u)" -eq 0 ] || die "run this as root"
if [ -z "$DOMAIN" ]; then                       # default to the server's own hostname (Hostinger gives srvNNN.hstgr.cloud)
  h="$(hostname -f 2>/dev/null || hostname)"
  case "$h" in *.*) DOMAIN="$h" ;; esac
fi
[ -n "$DOMAIN" ] || die "tell me the hostname: DOMAIN=attendance.yourdomain.com bash install.sh"
IP="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"

say "1/5 Installing Docker and git"
run apt-get update -y
run apt-get install -y git curl openssl ca-certificates
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  run apt-get install -y docker.io docker-compose-v2 || true
  if [ "$DRY_RUN" != 1 ] && ! docker compose version >/dev/null 2>&1; then
    echo "apt did not give a working 'docker compose'; trying Docker's own installer"
    curl -fsSL https://get.docker.com | sh || true
  fi
  if [ "$DRY_RUN" != 1 ] && ! docker compose version >/dev/null 2>&1; then
    apt-get install -y docker-compose-plugin || apt-get install -y docker-compose || true
  fi
  if [ "$DRY_RUN" != 1 ] && ! docker compose version >/dev/null 2>&1; then
    die "could not install 'docker compose'. Send the lines above to Claude."
  fi
fi
run systemctl enable --now docker

say "2/5 Getting the code ($BRANCH)"
if [ -d "$DIR/.git" ]; then
  run git -C "$DIR" fetch origin "$BRANCH"
  run git -C "$DIR" checkout "$BRANCH"
  run git -C "$DIR" pull --ff-only origin "$BRANCH"
else
  run git clone --branch "$BRANCH" "$REPO" "$DIR"
fi
[ "$DRY_RUN" = 1 ] && mkdir -p "$DIR"
cd "$DIR"

say "3/5 Configuration"
NEW_PASSWORD=""
if [ -f .env ]; then
  echo "Keeping your existing .env"
else
  NEW_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)"
  cat > .env <<ENV
DOMAIN=$DOMAIN
PUBLIC_DOMAIN=$DOMAIN
ADMIN_PASSWORD=$NEW_PASSWORD
SECRET_KEY=$(openssl rand -hex 32)
TIMEZONE=$TIMEZONE
GEOFENCE_MODE=block
FKWEB_DEVICES=$DEVICES
COMPANY_NAME=$COMPANY_NAME
COMPANY_ADDRESS=
ENV
  chmod 600 .env
  printf 'admin password (first login): %s\n' "$NEW_PASSWORD" > /root/attendance-admin-password.txt 2>/dev/null || true
  chmod 600 /root/attendance-admin-password.txt 2>/dev/null || true
fi
mkdir -p data

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  say "Opening ports 80/443 in ufw"
  run ufw allow 22/tcp; run ufw allow 80/tcp; run ufw allow 443/tcp
fi

say "4/5 Building and starting (a few minutes the first time)"
run docker compose up -d --build

say "5/5 Waiting for the site to come up (HTTPS certificate takes up to a minute)"
ok=0
if [ "$DRY_RUN" != 1 ]; then
  for i in $(seq 1 40); do
    if curl -fsS -m 5 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" >/dev/null 2>&1; then ok=1; break; fi
    sleep 4
  done
fi

echo
if [ "$ok" = 1 ]; then echo "SUCCESS: the system is running."; else
  echo "The containers are started, but https://$DOMAIN did not answer yet."
  echo "Check:  cd $DIR && docker compose logs --tail 50 caddy app"
  echo "If Caddy cannot get a certificate for $DOMAIN, point your own subdomain's A record to $IP and re-run with DOMAIN=that.name"
fi
cat <<DONE

  Dashboard : https://$DOMAIN/
  Phone app : https://$DOMAIN/m/
  Login     : admin
  Password  : ${NEW_PASSWORD:-(unchanged, see $DIR/.env)}
  (also saved in /root/attendance-admin-password.txt, readable only by root)

  FINGERPRINT DEVICES: set each device's Webserver URL to  http://$IP   (plain http, mode FkWeb), then restart the device.
  Allowed device ids: $DEVICES

  Useful:  cd $DIR && docker compose logs -f app      (live log; look for punches)
           cd $DIR && git pull && docker compose up -d --build     (update)
DONE
