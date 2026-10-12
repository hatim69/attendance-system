#!/usr/bin/env bash
# One-shot installer for the attendance system on an Ubuntu/Debian VPS. Run as root (e.g. in Hostinger's Web console):
#   curl -fsSL https://raw.githubusercontent.com/hatim69/attendance-system/claude/eager-wright-hkels3/deploy/install.sh | DOMAIN=attendance.madarwala.cloud bash
# Re-running is safe: it updates the code and keeps your existing .env, database and selfies.
# Moving to a new address: point the new name's DNS at this server, then re-run with DOMAIN=<new name>. The old name keeps
# working for the fingerprint devices (and browsers on it are sent to the new one), so devices can be switched one by one.
#
# MODE (auto-detected): "caddy" = this system owns ports 80/443 (automatic HTTPS);
#                       "nginx" = nginx already serves other sites on this VPS, so it is NOT touched except for ONE new
#                                 site file, and the app runs behind it.
set -euo pipefail

REPO="${REPO:-https://github.com/hatim69/attendance-system.git}"
BRANCH="${BRANCH:-claude/eager-wright-hkels3}"
DIR="${DIR:-/opt/attendance-system}"
DOMAIN="${DOMAIN:-}"
COMPANY_NAME="${COMPANY_NAME:-Madarwala Tours and Travels}"
TIMEZONE="${TIMEZONE:-Asia/Kolkata}"
DEVICES="${DEVICES:-RSS202506120920,RSS202506120926,RSS202506120928}"   # Ranigunz, Qutbi Arcade, Mumbai Office
MODE="${MODE:-auto}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
LE_DIR="${LE_DIR:-/etc/letsencrypt}"
ACME_ROOT="${ACME_ROOT:-/var/www/attendance-acme}"
DRY_RUN="${DRY_RUN:-0}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
die() { echo "ERROR: $*" >&2; exit 1; }
listening() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -v docker-proxy || true; }

[ "$DRY_RUN" = 1 ] || [ "$(id -u)" -eq 0 ] || die "run this as root"
if [ -z "$DOMAIN" ] && [ -f "$DIR/.env" ]; then  # re-run without DOMAIN: keep the address already in use
  DOMAIN="$(sed -n 's/^DOMAIN=//p' "$DIR/.env" | head -1)"
fi
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
set_env() {   # set_env KEY VALUE: replace the line, or add it
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
if [ -f .env ]; then
  echo "Keeping your existing .env"
  OLD="$(sed -n 's/^DOMAIN=//p' .env | head -1)"
  if [ -n "$OLD" ] && [ "$OLD" != "$DOMAIN" ]; then
    # check DNS first, so a typo or a missing record never takes down the address that works today
    RESOLVED="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd' ' - || true)"
    if [ "$DRY_RUN" != 1 ] && [ "${SKIP_DNS_CHECK:-0}" != 1 ] && ! echo " $RESOLVED " | grep -qF " $IP "; then
      die "$DOMAIN does not point at this server yet (it resolves to: ${RESOLVED:-nothing}; this server is $IP).
Add a DNS 'A' record: name = ${DOMAIN%%.*}, points to = $IP. Wait a few minutes and run this again.
Nothing was changed: $OLD still works."
    fi
    echo "Changing the address from $OLD to $DOMAIN ($OLD keeps working for the fingerprint devices)"
    PREV="$(sed -n 's/^OLD_DOMAINS=//p' .env | head -1)"
    ALL="$(printf '%s\n' $(echo "$PREV,$OLD" | tr ',' ' ') | grep -vxF "$DOMAIN" | awk 'NF && !seen[$0]++' | paste -sd, - || true)"
    set_env DOMAIN "$DOMAIN"; set_env PUBLIC_DOMAIN "$DOMAIN"; set_env OLD_DOMAINS "$ALL"
  fi
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

if [ "$MODE" = auto ]; then
  if [ "$DRY_RUN" != 1 ] && [ -n "$(listening 80 | grep nginx || true)" ]; then MODE=nginx; else MODE=caddy; fi
fi
echo "Mode: $MODE"
# a half-finished earlier attempt may have left containers behind; this never touches data
run docker compose -f docker-compose.yml down --remove-orphans >/dev/null 2>&1 || true

# --------------------------------------------------------------------------------------------------------------------
write_nginx_site() {   # $1 = "http" (no certificate yet) or "https"
  local f="$NGINX_DIR/sites-available/attendance-system"
  local olds; olds="$(sed -n 's/^OLD_DOMAINS=//p' .env 2>/dev/null | head -1 | tr ',' ' ')"
  mkdir -p "$NGINX_DIR/sites-available" "$NGINX_DIR/sites-enabled" "$ACME_ROOT"
  cat > "$f" <<NGINX
# Attendance system. Generated by deploy/install.sh. Remove with: rm $NGINX_DIR/sites-enabled/attendance-system && nginx -t && systemctl reload nginx
server {
    listen 80;
    server_name $DOMAIN${olds:+ $olds};
    underscores_in_headers on;          # the fingerprint devices send headers such as request_code and dev_id
    client_max_body_size 8m;

    location /.well-known/acme-challenge/ { root $ACME_ROOT; }

    location / {
        # browsers -> https; fingerprint devices (they send a request_code header, and cannot do https) -> the app
        if (\$http_request_code = "") { return 301 https://$DOMAIN\$request_uri; }
        proxy_pass http://127.0.0.1:$FK_PORT;
    }
}
NGINX
  if [ "$1" = https ]; then
    cat >> "$f" <<NGINX

server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate     $LE_DIR/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key $LE_DIR/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_read_timeout 3600s;
    }
}
NGINX
    for o in $olds; do   # an old name that already has a certificate: send its https visitors to the new name
      [ -f "$LE_DIR/live/$o/fullchain.pem" ] || continue
      cat >> "$f" <<NGINX

server {
    listen 443 ssl;
    server_name $o;
    ssl_certificate     $LE_DIR/live/$o/fullchain.pem;
    ssl_certificate_key $LE_DIR/live/$o/privkey.pem;
    return 301 https://$DOMAIN\$request_uri;
}
NGINX
    done
  fi
}

apply_nginx() {        # test the whole config first; on any problem remove our file so existing sites are untouched
  if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] nginx -t && systemctl reload nginx"; return 0; fi
  if nginx -t >/tmp/nginx-test.log 2>&1; then
    systemctl reload nginx
  else
    cat /tmp/nginx-test.log
    rm -f "$NGINX_DIR/sites-enabled/attendance-system" "$NGINX_DIR/sites-available/attendance-system"
    die "nginx rejected the new site, so I removed it. Your other sites were not changed."
  fi
}

install_nginx_mode() {
  say "4/5 Sharing this server with nginx (your existing sites are not touched)"
  # other apps on this server may already use ports (yours has gunicorn on 8000): pick free localhost ports for ours
  pick_port() { local p="$1"; while [ -n "$(listening "$p")" ]; do p=$((p + 1)); done; echo "$p"; }
  APP_PORT="$(sed -n 's/^APP_PORT=//p' .env | head -1)"; FK_PORT="$(sed -n 's/^FK_PORT=//p' .env | head -1)"
  [ -n "$APP_PORT" ] || APP_PORT="$(pick_port 18000)"
  [ -n "$FK_PORT" ] || FK_PORT="$(pick_port 18081)"
  grep -q '^APP_PORT=' .env || echo "APP_PORT=$APP_PORT" >> .env
  grep -q '^FK_PORT=' .env || echo "FK_PORT=$FK_PORT" >> .env
  echo "Using localhost ports $APP_PORT (dashboard) and $FK_PORT (devices)"
  # nginx needs this map for websocket upgrades (the live dashboard feed); put it in its own file, only if missing
  if ! grep -rqs "connection_upgrade" "$NGINX_DIR" 2>/dev/null; then
    mkdir -p "$NGINX_DIR/conf.d"
    printf 'map $http_upgrade $connection_upgrade { default upgrade; "" close; }\n' > "$NGINX_DIR/conf.d/attendance-websocket-map.conf"
  fi
  run env APP_PORT="$APP_PORT" FK_PORT="$FK_PORT" docker compose -f docker-compose.nginx.yml up -d --build
  write_nginx_site http
  [ "$DRY_RUN" = 1 ] || ln -sf "$NGINX_DIR/sites-available/attendance-system" "$NGINX_DIR/sites-enabled/attendance-system"
  apply_nginx
  say "5/5 HTTPS certificate for $DOMAIN"
  command -v certbot >/dev/null 2>&1 || run apt-get install -y certbot
  have_cert=0
  [ -f "$LE_DIR/live/$DOMAIN/fullchain.pem" ] && have_cert=1
  if [ "$have_cert" = 0 ] && [ "$DRY_RUN" != 1 ]; then
    certbot certonly --webroot -w "$ACME_ROOT" -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email \
      && have_cert=1 || true
  fi
  if [ "$have_cert" = 1 ] || [ "$DRY_RUN" = 1 ]; then
    write_nginx_site https
    apply_nginx
  else
    echo "Could not get an HTTPS certificate for $DOMAIN (is its DNS pointing at $IP?)."
    echo "The fingerprint devices will already work over http. For the dashboard, add an A record for a subdomain of yours"
    echo "(e.g. attendance.madarwala.cloud -> $IP), then re-run with DOMAIN=attendance.madarwala.cloud"
  fi
  HEALTH_OK=0
  if [ "$DRY_RUN" != 1 ] && [ "$have_cert" = 1 ]; then
    for i in $(seq 1 20); do
      curl -fsS -m 5 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" >/dev/null 2>&1 && { HEALTH_OK=1; break; }
      sleep 3
    done
  fi
  DEVICE_URL="http://$DOMAIN"
}

install_caddy_mode() {
  say "4/5 Checking ports 80 and 443 are free"
  busy=""
  if [ "$DRY_RUN" != 1 ]; then
    for port in 80 443; do
      owner="$(listening "$port")"
      [ -n "$owner" ] && busy="$busy\n  port $port: $owner"
    done
  fi
  if [ -n "$busy" ]; then
    echo "Something other than nginx is already using a port the system needs:"; printf "$busy\n"
    die "free those ports (or tell Claude what they are) and run this installer again"
  fi
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    run ufw allow 22/tcp; run ufw allow 80/tcp; run ufw allow 443/tcp
  fi
  say "5/5 Building and starting (a few minutes the first time)"
  run docker compose up -d --build
  HEALTH_OK=0
  if [ "$DRY_RUN" != 1 ]; then
    for i in $(seq 1 40); do
      curl -fsS -m 5 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" >/dev/null 2>&1 && { HEALTH_OK=1; break; }
      sleep 4
    done
  fi
  DEVICE_URL="http://$IP"
}

HEALTH_OK=0; DEVICE_URL=""
if [ "$MODE" = nginx ]; then install_nginx_mode; else install_caddy_mode; fi

echo
if [ "$HEALTH_OK" = 1 ]; then echo "SUCCESS: the system is running."; else
  echo "Started, but https://$DOMAIN did not answer yet. Check:  cd $DIR && docker compose logs --tail 50"
fi
cat <<DONE

  Dashboard : https://$DOMAIN/
  Phone app : https://$DOMAIN/m/
  Login     : admin   (password below is the first-login one; ignore it if you already changed it)
  Password  : ${NEW_PASSWORD:-$(sed -n 's/^admin password (first login): //p' /root/attendance-admin-password.txt 2>/dev/null | head -1)}
  (also saved in /root/attendance-admin-password.txt, readable only by root)

  FINGERPRINT DEVICES: set each device's Webserver URL to  $DEVICE_URL   (plain http, mode FkWeb), then restart the device.
$( o="$(sed -n 's/^OLD_DOMAINS=//p' .env 2>/dev/null | head -1)"; [ -n "$o" ] && echo "  (devices still set to $o keep working until you change them)" )
  Allowed device ids: $DEVICES

  Useful:  cd $DIR && docker compose logs -f app      (live log; look for punches)
           cd $DIR && git pull && docker compose up -d --build     (update; add -f docker-compose.nginx.yml in nginx mode)
DONE
