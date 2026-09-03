# Deployment

The trading agent listens on **no port**. It makes outbound connections only —
websockets to Alpaca, HTTPS to the model providers, and gRPC to the OTel collector.
The separate read-only demo panel binds `0.0.0.0` on TCP 7001; UFW permits that
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

## Production deployment

```bash
./deploy/deploy.sh --mode execute
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
| production paper account | `alpaca-agent.service` | `/opt/alpaca-agent` | competition, execute, balanced 10% risk profile | `alpaca-panel.service`, 7001 |

The agent and panel are separate services. The panel reads the run directory only
and has no broker action path.

The balanced profile caps robust, aligned-directional, single-position and correlated
scenario risk at 10% of equity. Aggregate premium at risk is capped at 30%; the 3.5%
build target schedules review but does not force deployment or impose a minimum
position size. There is no realised-loss recovery multiplier. Quote, liquidity,
economics, exact-candidate evidence, fresh-price, direction, concentration and
resulting-book gates remain mandatory.

The operator-authorized post-submission session ends Friday 4 September at 16:00 ET.
The ordinary Friday entry cutoff is 15:45 ET. After the absolute end timestamp the
host permanently refuses new entries; keeping the service enabled preserves exit,
reconciliation and audit monitoring without reopening trading next week.
Tier-2 cycles have no fixed per-session count cap; trigger debounce, event dedupe and
the three-per-session blocked-trigger escalation cap still bound repeated work.
Deterministic Tier-0 exits remain independent of model availability.

## Watching

```bash
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'journalctl -u alpaca-agent -f'
ssh -i ~/.ssh/alpaca_agent_vps root@185.102.78.75 'tail -f /opt/alpaca-agent/.run/agent.log'
```

## Only one production instance

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
PYTHONPATH=src .venv/bin/python scripts/panel.py --run-dir .run/live --port 7001
```

In the present hackathon paper-demo deployment:

```bash
# competition: http://185.102.78.75:7001
```

This is an unauthenticated public view of a paper account. For any non-demo use,
change the unit to `--host 127.0.0.1`, close UFW 7001, and reach it through
an SSH tunnel or an authenticated reverse proxy.
