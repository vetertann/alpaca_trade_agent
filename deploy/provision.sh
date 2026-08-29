#!/usr/bin/env bash
# Provision the VPS. Idempotent. Touches nothing that already runs there:
# xray (443), mtproxy (1443), control.service (3000) and their data are untouched,
# and no UFW rule is added because the agent listens on no port.
set -euo pipefail

APP=/opt/alpaca-agent
USER=alpaca

echo "== existing services, for the record =="
systemctl is-active xray.service mtproxy.service control.service 2>/dev/null || true
ss -lntp | grep -E ':(443|1443|3000|22)\b' || true

echo "== packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv python3-pip git curl >/dev/null

echo "== service user =="
id -u "$USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$USER"

echo "== directories =="
mkdir -p "$APP"/{src,scripts,.run}
chown -R "$USER:$USER" "$APP"
chmod 750 "$APP"

echo "== python env =="
if [ ! -x "$APP/.venv/bin/python" ]; then
  python3.12 -m venv "$APP/.venv"
fi
"$APP/.venv/bin/pip" install -q --upgrade pip
chown -R "$USER:$USER" "$APP/.venv"

echo "== done. nothing else was modified =="
ufw status | head -12 || true
