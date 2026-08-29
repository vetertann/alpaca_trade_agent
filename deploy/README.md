# Deployment

The agent listens on **no port**. It makes outbound connections only — websockets to
Alpaca, HTTPS to the model providers, gRPC to the OTel collector. No UFW rule is
needed and none is added.

Existing services on the box are untouched: `xray` (443), `mtproxy` (1443),
`control.service` (3000) and its database.

## First time

```bash
ssh-copy-id -i ~/.ssh/alpaca_agent_vps.pub root@185.102.78.75   # you type the password
scp -i ~/.ssh/alpaca_agent_vps deploy/provision.sh root@185.102.78.75:/tmp/
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'bash /tmp/provision.sh'
scp -i ~/.ssh/alpaca_agent_vps .env root@185.102.78.75:/opt/alpaca-agent/.env
```

## Every time

```bash
./deploy/deploy.sh --profile dev --mode propose
```

Going live Monday:

```bash
./deploy/deploy.sh --profile competition --mode execute
```

## Watching

```bash
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'journalctl -u alpaca-agent -f'
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'tail -f /opt/alpaca-agent/.run/agent.log'
```

## Only one agent, anywhere

Alpaca refuses a second stream connection per feed per account with 406, and the
incumbent wins. Once this is running, do not start one on the laptop against the
same account — it would fail quietly and look like it was working.

```bash
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'systemctl stop alpaca-agent'
```

## Hardening applied

Dedicated `alpaca` system user with `nologin`, `ProtectSystem=strict`,
`ProtectHome`, `NoNewPrivileges`, `PrivateTmp`, writable path limited to the run
directory, 1 GB memory cap, and a restart limit of 5 in 10 minutes so a crash loop
cannot hammer Alpaca.

## Panel

A read-only view of the agent, served by a separate process. It reads the JSONL
trace and nothing else — it cannot place, cancel, or influence a trade, and `POST`
returns 405.

Locally:

```bash
PYTHONPATH=src .venv/bin/python scripts/panel.py --run-dir .run/live --port 3001
```

On the server it binds `127.0.0.1` by default, so reach it over an SSH tunnel
rather than opening a firewall port:

```bash
ssh -i ~/.ssh/alpaca_agent_vps -L 3001:127.0.0.1:3001 root@185.102.78.75
# then open http://127.0.0.1:3001
```

Binding `0.0.0.0` and opening UFW 3001 would put an unauthenticated view of the
account on the public internet. The tunnel costs nothing and avoids that.
