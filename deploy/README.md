# Deployment

The trading agent listens on **no port**. It makes outbound connections only —
websockets to Alpaca, HTTPS to the model providers, and gRPC to the OTel collector.
The separate read-only demo panel binds `0.0.0.0` on TCP 3001; UFW permits that
port. The panel reads run artifacts only, exposes no credentials, rejects
all `POST` requests, and cannot place or cancel a trade.

Existing services on the box are untouched: `xray` (443), `mtproxy` (1443),
`control.service` (3000) and its database.

## First time

```bash
ssh-copy-id -i ~/.ssh/alpaca_agent_vps.pub root@185.102.78.75   # you type the password
scp -i ~/.ssh/alpaca_agent_vps deploy/provision.sh root@185.102.78.75:/tmp/
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'bash /tmp/provision.sh'
scp -i ~/.ssh/alpaca_agent_vps .env root@185.102.78.75:/opt/alpaca-agent/.env
```

## Competition deployment

```bash
./deploy/deploy.sh --profile competition --mode execute
```

`deploy.sh` targets `/opt/alpaca-agent` and restarts only
`alpaca-agent.service`.

For a release containing risk-engine changes, first sync it to a separate
timestamped `/opt/alpaca-agent-stage-*` directory and run the complete suite there.
A stage may reuse the installed dependency environment, but it must use its own run
directory and `--mode propose`; never point a stage process at the live ledger.
Record broker open-order and position counts before and after the smoke check. They
must be identical. Do not leave the stage process running: Alpaca permits only one
stream owner, so the live service remains the sole long-lived process.

## Production topology

| Role | Unit | Directory | Arguments | Panel |
|---|---|---|---|---|
| judged competition account | `alpaca-agent.service` | `/opt/alpaca-agent` | competition, execute, high-variance tournament profile | `alpaca-panel.service`, 3001 |

The agent and panel are separate services. The panel reads the run directory only
and has no broker action path.

The high-variance profile makes 10% of equity maximum loss available to a robust
or directionally aligned position, permits 30% aggregate premium at risk, throttles
new entries after 15% realised loss, and continues build reviews below 9% correlated
scenario risk. These are ceilings, not forced allocations: quote, liquidity,
economics, evidence, concentration, and resulting-book gates remain mandatory.
The unit also selects the `high_variance` sizing posture, which tells the model to
aim for 7–10% maximum-loss sizing when evidence is robust and to name a concrete
reason when choosing less. The default posture remains `balanced`.

## Watching

```bash
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'journalctl -u alpaca-agent -f'
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'tail -f /opt/alpaca-agent/.run/agent.log'
```

## Only one agent

Alpaca refuses a second stream connection per feed/account with 406, and the
incumbent wins. Once the server unit is running, do not start a laptop process
against the same account—it would fail quietly and look like it was working.

```bash
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 \
  'systemctl status alpaca-agent alpaca-panel'
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

In the present hackathon paper-demo deployment:

```bash
# competition: http://185.102.78.75:3001
```

This is an unauthenticated public view of a paper account. For any non-demo use,
change the unit to `--host 127.0.0.1`, close UFW 3001, and reach it through
an SSH tunnel or an authenticated reverse proxy.
