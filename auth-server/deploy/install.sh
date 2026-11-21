#!/usr/bin/env bash
# One-step setup of the login server on a fresh Ubuntu/Debian VPS. Run as root:
#
#   curl -fsSL https://raw.githubusercontent.com/dansolutions2017-coder/Channelbox/login-server/auth-server/deploy/install.sh | bash -s -- auth.yourdomain.com
#
# Safe to run again: it updates the code and restarts, keeping your settings and data.
set -euo pipefail

REPO="https://github.com/dansolutions2017-coder/Channelbox"
BRANCH="login-server"
DIR="/opt/channelbox"
APP="$DIR/auth-server"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mXX  %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this as root (or put sudo in front of bash)."
command -v apt-get >/dev/null || die "This script is for Ubuntu/Debian."

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ] && [ -r /dev/tty ]; then
  read -rp "Address for the login server (e.g. auth.yourdomain.com): " DOMAIN < /dev/tty
fi
DOMAIN="$(echo "$DOMAIN" | tr -d '[:space:]' | sed -E 's#^https?://##; s#/.*$##')"
[ -n "$DOMAIN" ] || die "No address given."

say "Installing updates, git, firewall"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw openssl dnsutils >/dev/null

say "Installing Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true

say "Firewall: allowing SSH, HTTP and HTTPS only"
# Keep whatever port SSH is really on open (so PuTTY never gets locked out).
SSH_PORTS="$(ss -tlnpH 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}' | sort -u)"
for p in ${SSH_PORTS:-22} 22; do ufw allow "$p/tcp" >/dev/null; done
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

say "Getting the login server code"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q --depth 1 origin "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q --depth 1 -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$APP"

say "Settings"
mkdir -p data config
NEW_PASSWORD=""
if [ ! -f .env ]; then
  cp .env.example .env
  NEW_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  sed -i "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=$NEW_PASSWORD#" .env
fi
sed -i "s#^AUTH_DOMAIN=.*#AUTH_DOMAIN=$DOMAIN#" .env
[ -f config/app_config.json ] || cp config/app_config.example.json config/app_config.json
[ -f config/servers.json ] || echo '{ "servers": [] }' > config/servers.json
chown -R 10001 data config

say "Starting (first time takes a couple of minutes)"
docker compose up -d --build

say "Checking"
MY_IP="$(curl -fsS4 --max-time 5 https://api.ipify.org || true)"
DNS_IP="$(dig +short A "$DOMAIN" | tail -n1 || true)"
sleep 5
if curl -fsS --max-time 5 http://127.0.0.1/healthz -H "Host: $DOMAIN" >/dev/null 2>&1 \
   || docker compose ps --status running | grep -q auth; then
  echo "Login server is running."
else
  warn "The server didn't start. Show the log with:  cd $APP && docker compose logs --tail 50"
fi
if [ -z "$DNS_IP" ]; then
  warn "$DOMAIN doesn't point anywhere yet. In your DNS (GoDaddy), add an A record:"
  warn "    Name: ${DOMAIN%%.*}    Value: ${MY_IP:-the server IP}"
  warn "HTTPS starts working on its own a few minutes after that."
elif [ -n "$MY_IP" ] && [ "$DNS_IP" != "$MY_IP" ]; then
  warn "$DOMAIN points to $DNS_IP, but this server is $MY_IP. Fix the A record in your DNS."
else
  echo "DNS is correct ($DOMAIN -> $DNS_IP)."
fi

echo
echo "------------------------------------------------------------"
echo " Admin page:  https://$DOMAIN/admin"
if [ -n "$NEW_PASSWORD" ]; then
echo " Password:    $NEW_PASSWORD"
echo "              (write it down; it's also in $APP/.env)"
else
echo " Password:    unchanged (see ADMIN_PASSWORD in $APP/.env)"
fi
echo " Update later: run the same command again."
echo "------------------------------------------------------------"
