#!/usr/bin/env bash
# Basic security hardening for the login-server VPS (Ubuntu/Debian). Run as root:
#
#   curl -fsSL https://raw.githubusercontent.com/dansolutions2017-coder/Channelbox/login-server/auth-server/deploy/harden.sh | bash
#
# Safe to run again. It never locks you out: password logins are only switched off
# once an SSH key is installed for root (run it again after adding your PuTTY key).
set -euo pipefail

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m OK %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m XX %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this as root."
command -v apt-get >/dev/null || die "This script is for Ubuntu/Debian."
export DEBIAN_FRONTEND=noninteractive

say "Installing security tools"
apt-get update -qq
apt-get install -y -qq unattended-upgrades fail2ban python3-systemd ufw sqlite3 >/dev/null
ok "unattended-upgrades, fail2ban, ufw"

say "Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
cat > /etc/apt/apt.conf.d/52channelbox-upgrades <<'EOF'
// Restart at 04:00 server time only when an update needs it (e.g. a new kernel).
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
ok "Security updates install daily; reboots (if ever needed) at 04:00"

say "Firewall"
SSH_PORTS="$(ss -tlnpH 2>/dev/null | awk '/sshd/ {n=split($4,a,":"); print a[n]}' | sort -u)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
for p in ${SSH_PORTS:-22} 22; do ufw allow "$p/tcp" >/dev/null; done
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
ok "Only SSH (${SSH_PORTS:-22}), HTTP and HTTPS are open"

say "Blocking password guessers (fail2ban)"
cat > /etc/fail2ban/jail.d/channelbox.local <<EOF
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
banaction = ufw

[sshd]
enabled = true
port    = ${SSH_PORTS:-22}
backend = systemd
EOF
systemctl enable fail2ban >/dev/null 2>&1 || true
if systemctl restart fail2ban; then
  ok "5 wrong SSH logins in 10 minutes = that IP is blocked for 1 hour"
else
  warn "fail2ban didn't start. Show why with:  journalctl -u fail2ban --no-pager | tail -20"
fi

say "SSH settings"
mkdir -p /etc/ssh/sshd_config.d
HAS_KEY=no
if [ -s /root/.ssh/authorized_keys ] && grep -qE '^(ssh-|ecdsa-|sk-)' /root/.ssh/authorized_keys; then
  HAS_KEY=yes
fi
{
  echo "# Written by harden.sh"
  echo "MaxAuthTries 3"
  echo "LoginGraceTime 30"
  echo "X11Forwarding no"
  echo "PermitEmptyPasswords no"
  if [ "$HAS_KEY" = yes ]; then
    echo "PasswordAuthentication no"
    echo "KbdInteractiveAuthentication no"
    echo "PermitRootLogin prohibit-password"
  fi
} > /etc/ssh/sshd_config.d/10-channelbox.conf
if sshd -t 2>/dev/null; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  if [ "$HAS_KEY" = yes ]; then
    ok "Password logins are OFF; only your SSH key can log in"
  else
    ok "Fewer login attempts allowed per connection"
    warn "Password logins are still ON because no SSH key is installed yet."
    warn "Add your PuTTY key (see the README), then run this script again."
  fi
else
  rm -f /etc/ssh/sshd_config.d/10-channelbox.conf
  warn "SSH settings were not accepted, so nothing was changed there."
fi

say "Docker log limits (so logs can't fill the disk)"
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
  systemctl restart docker
  (cd /opt/channelbox/auth-server 2>/dev/null && docker compose up -d >/dev/null 2>&1) || true
  ok "Each container keeps at most 30 MB of logs"
else
  ok "Already configured"
fi

say "Daily backup of the login server's data"
cat > /etc/cron.daily/channelbox-backup <<'EOF'
#!/bin/sh
# Keeps 14 days of backups in /root/backups (database + server list + settings).
set -e
D=/root/backups; mkdir -p "$D"; chmod 700 "$D"
A=/opt/channelbox/auth-server
T=$(date +%F)
[ -f "$A/data/auth.db" ] && sqlite3 "$A/data/auth.db" ".backup '$D/auth-$T.db'"
tar -czf "$D/config-$T.tgz" -C "$A" config .env 2>/dev/null || true
find "$D" -type f -mtime +14 -delete
EOF
chmod 700 /etc/cron.daily/channelbox-backup
/etc/cron.daily/channelbox-backup || true
ok "Backups in /root/backups, kept 14 days"

say "Protecting the settings file"
[ -f /opt/channelbox/auth-server/.env ] && chmod 600 /opt/channelbox/auth-server/.env && ok ".env readable by root only"

echo
echo "------------------------------------------------------------"
echo " Done. Run this again any time; it's safe."
if [ "$HAS_KEY" = no ]; then
echo " Next step for full security: set up an SSH key in PuTTY,"
echo " then run this script again to switch off password logins."
fi
echo "------------------------------------------------------------"
