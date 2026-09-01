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
  pyproject.toml "$HOST:$APP/"
rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" \
  --exclude '__pycache__' --exclude '*.pyc' \
  scripts/ "$HOST:$APP/scripts/"

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

echo "== official Alpaca CLI =="
$SSH 'set -e
CLI_VERSION=0.0.14
CLI_ARCH=$(uname -m)
case "$CLI_ARCH" in
  x86_64) CLI_ASSET=cli_0.0.14_linux_amd64.tar.gz; CLI_SHA=6c82ef31f94dd61aae1c90e40fc41fdfaf8111bd50e9a2780b9d8d304eb2ba66 ;;
  aarch64|arm64) CLI_ASSET=cli_0.0.14_linux_arm64.tar.gz; CLI_SHA=621270e2b935dbae587e6ae05fe04a10bc178b4c9c638961a3d0214568ff2617 ;;
  *) echo "unsupported CLI architecture: $CLI_ARCH" >&2; exit 1 ;;
esac
CLI_TMP=$(mktemp -d)
trap '\''rm -rf "$CLI_TMP"'\'' EXIT
curl -fsSL "https://github.com/alpacahq/cli/releases/download/v${CLI_VERSION}/${CLI_ASSET}" -o "$CLI_TMP/$CLI_ASSET"
printf "%s  %s\n" "$CLI_SHA" "$CLI_TMP/$CLI_ASSET" | sha256sum -c -
tar -xzf "$CLI_TMP/$CLI_ASSET" -C "$CLI_TMP"
install -m 0755 "$CLI_TMP/alpaca" /usr/local/bin/alpaca
/usr/local/bin/alpaca version'

echo "== service unit =="
scp -q -i "$KEY" deploy/alpaca-agent.service "$HOST:/etc/systemd/system/alpaca-agent.service"
$SSH "sed -i 's/^Environment=PYTHONPATH.*/&/' /etc/systemd/system/alpaca-agent.service"
$SSH "grep -q AGENT_PROFILE $APP/.env || printf 'AGENT_PROFILE=%s\nAGENT_MODE=%s\n' '$PROFILE' '$MODE' >> $APP/.env"
$SSH "sed -i 's/^AGENT_PROFILE=.*/AGENT_PROFILE=$PROFILE/; s/^AGENT_MODE=.*/AGENT_MODE=$MODE/' $APP/.env"
$SSH "chown -R alpaca:alpaca $APP && chmod 640 $APP/.env"
$SSH "systemctl daemon-reload && systemctl restart alpaca-agent && sleep 3 && systemctl is-active alpaca-agent"

echo "== status =="
$SSH "systemctl status alpaca-agent --no-pager -l | head -14"
