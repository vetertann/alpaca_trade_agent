#!/usr/bin/env bash
# Push code and restart. Run from the repo root on the laptop.
#   ./deploy/deploy.sh [--mode propose|execute] [--profile dev|competition]
set -euo pipefail

HOST=${HOST:-root@185.102.78.75}
KEY=${KEY:-$HOME/.ssh/alpaca_agent_vps}
APP=/opt/alpaca-agent
MODE=propose
PROFILE=dev

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE=$2; shift 2 ;;
    --profile) PROFILE=$2; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done

SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new $HOST"

echo "== syncing source (no secrets, no run artifacts) =="
rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" \
  --exclude '__pycache__' --exclude '*.pyc' \
  src/ "$HOST:$APP/src/"
rsync -az -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" \
  pyproject.toml scripts/ "$HOST:$APP/" 2>/dev/null || true

echo "== dependencies =="
$SSH "$APP/.venv/bin/pip install -q -r /dev/stdin" <<'REQ'
alpaca-py>=0.42
anthropic>=1.2
openai>=3.0
httpx>=0.27
websockets>=15
msgpack>=1.0
numpy>=2.0
pandas>=2.2
scipy>=1.14
python-dotenv>=1.0
opentelemetry-api>=1.44
opentelemetry-sdk>=1.44
opentelemetry-exporter-otlp-proto-grpc>=1.44
REQ

echo "== service unit =="
scp -q -i "$KEY" deploy/alpaca-agent.service "$HOST:/etc/systemd/system/alpaca-agent.service"
$SSH "sed -i 's/^Environment=PYTHONPATH.*/&/' /etc/systemd/system/alpaca-agent.service"
$SSH "grep -q AGENT_PROFILE $APP/.env || printf 'AGENT_PROFILE=%s\nAGENT_MODE=%s\n' '$PROFILE' '$MODE' >> $APP/.env"
$SSH "sed -i 's/^AGENT_PROFILE=.*/AGENT_PROFILE=$PROFILE/; s/^AGENT_MODE=.*/AGENT_MODE=$MODE/' $APP/.env"
$SSH "chown -R alpaca:alpaca $APP && chmod 640 $APP/.env"
$SSH "systemctl daemon-reload && systemctl restart alpaca-agent && sleep 3 && systemctl is-active alpaca-agent"

echo "== status =="
$SSH "systemctl status alpaca-agent --no-pager -l | head -14"
