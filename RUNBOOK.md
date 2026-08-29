# Monday runbook

Scored window opens **Monday 31 August, 09:30 ET**. Everything below is ordered by
when it has to happen.

## Known risk: no rehearsal

The system has never met a live tape. Every live check to date ran against Friday's
closing quotes, which are stale by design and wider than intraday. Specifically
untested until Monday:

- an option quote arriving over the websocket
- an order filling
- the repricing ladder in `manage_fill`
- the rolling series producing an intraday realized-vol figure

The warm-up protocol below exists to compress that risk into the fifteen minutes
before entries open, on the dev account.

## Before the open

```bash
cd /Users/ivan/Documents/Hackatons/Alpaca
set -a; . ./.env; set +a
.venv/bin/python -m pytest tests/ -q          # expect 169 passed
```

Confirm the competition account is still untouched — this must read zero:

```bash
PYTHONPATH=src .venv/bin/python -c "
from agent.config import load_env, profile; from agent.host.rest import Rest
load_env(); r = Rest(profile('competition'))
a = r.account()
print('equity', a['equity'], '| positions', len(r.positions()),
      '| orders ever', len(r.orders('all')))"
```

## 09:30–09:45 ET — warm-up protocol

**This is the only live-market rehearsal we get.** Friday 28 August has closed, the
market is shut all weekend, and the next open is Monday 09:30 — which is the scored
window itself. Entries are blocked until 09:45, so the warm-up is the window for it.

```bash
PYTHONPATH=src .venv/bin/python scripts/warmup_check.py --order --seconds 45
```

Eight checks, four of which cannot pass without a live tape:

| Check | Why it needs a live market |
|---|---|
| equity quotes flowing | connected is not delivering |
| option quotes flowing | the indicative feed has never delivered a quote to us |
| rolling series filling | the whole `diff` block and every threshold depends on it |
| quote freshness | staleness budget is 90s; Saturday reads 80,598s |
| validity gate accepts live quotes | on stale data it correctly rejects everything |
| spreads inside the gate | closing spreads are wider than intraday |
| **order round trip** | revalidates submit/cancel against the live order service |
| no position left behind | proves cancel actually cancelled |

`--order` stages a far out-of-the-money debit spread on the **dev** account, submits
the same geometry at a deliberately non-marketable one-cent debit, checks it appears
with our `client_order_id`, cancels it in a `finally` block, and confirms nothing is
left. The competition account is never touched.

The durable execution lifecycle is rehearsed locally before this live check:

```bash
.venv/bin/python -m pytest tests/test_rehearsal.py tests/test_ledger.py -q
```

That deterministic broker rehearsal covers partial fill, cancellation, process
restart, realized-loss recovery, and forced final-session liquidation—states that
cannot be reliably manufactured against the paper venue on demand.

The closed-market control-plane rehearsal on 2026-08-29 was accepted by the dev
paper account at a one-cent debit and then reached `canceled`; the postcondition was
zero fills, zero open orders, and zero positions.

It prints a GO/NO-GO. Do not start the competition agent on a NO-GO.

Expect a partial pass at 09:30 and a full pass by 09:40 — the rolling series needs a
few minutes of quotes before it has anything to report.

## 09:35 ET — calibrate, before the first entry

Closing quotes are systematically wider than intraday, so every parameter measured
on Saturday is an upper bound.

```bash
PYTHONPATH=src .venv/bin/python scripts/calibrate.py --profile dev --apply
```

Read two things off it:

1. **`recommended MAX_SPREAD_PCT_OF_MID`.** Currently `8.9`, set from Saturday's
   at-the-money p90 of 5.95%. If the live figure is materially lower, edit
   `src/agent/host/risk_params.py`. Too tight blocks every candidate; too loose lets
   the crossing cost eat the edge.
2. **`iv/rv` per underlying.** Saturday read SPY 0.83 and QQQ 0.67 — implied below
   realized, long premium favoured. This is computed from stale closes. If it holds
   on live quotes the read stands; if it inverts, the opportunity family changes and
   nothing in the agent needs editing for that.

Also check the zero-bid share. Saturday: SPY 7%, QQQ 2.5%, **IWM 18%**. If IWM is
still that thin, leave it out of the traded set.

## 09:40 ET — dry run on the competition account

Propose mode places no orders. This proves the identity gate passes now that the
window is open, and that the whole path works against the real account.

```bash
PYTHONPATH=src .venv/bin/python -m agent.run \
  --profile competition --mode propose --once --run-dir .run/monday-dry
```

Expect `account_identity: PASS` in the checklist. On Saturday the same call fails
with *"competition account addressed outside the scored window"*, which is correct.

## 09:45 ET — go live

On the server, which is where it should run for a four-day unattended window:

```bash
./deploy/deploy.sh --profile competition --mode execute
```

Or locally, if the server is not in play:

```bash
PYTHONPATH=src nohup .venv/bin/python -m agent.run \
  --profile competition --mode execute --run-dir .run/live \
  > .run/live/stdout.log 2>&1 &
```

`--mode execute` submits orders. `--dev-models` is deliberately absent: the decision
role runs on `claude-opus-5`.

**Run it in exactly one place.** Alpaca refuses a second stream connection per feed
per account with 406 and the incumbent wins, so a second process fails quietly while
looking healthy. If the server agent is live, do not start a local one.

## Telemetry

Traces go to the OTel collector at `COLLECTOR_HOST` over OTLP/gRPC on 4317, service
name `alpaca_options_agent`. One decision cycle is one trace:

```
invoke_agent alpaca_options_agent
  chat claude-opus-5              the decision, with token usage and reasoning
  execute_tool run_program        the program, carrying the chat's tool call id
    execute_tool market.spot      every capability call the program made
    execute_tool options.enumerate
    ...
```

Off by default in content terms — `OTEL_CAPTURE_CONTENT=true` additionally sends
prompts, generated programs, reasoning summaries, and tool arguments. Leave it off
unless you want the full payload in the collector.

Emission never blocks trading: setup failure prints and disables, and every span
helper is a no-op when disabled.

Raw traces are visible at `http://$COLLECTOR_HOST:8080` under the **Agent Traces**
source, and folded rows land in the `obt_alpaca_options_agent` table.

## Watching it

```bash
tail -f .run/live/stdout.log

PYTHONPATH=src .venv/bin/python -c "
from agent.host.trace import Trace
for r in Trace('.run/live/trace.jsonl').records()[-30:]:
    print(r['kind'], str({k:v for k,v in r.items()
                          if k in ('outcome','reason','message','unhealthy')})[:150])"
```

Stream health is written to the trace every five minutes. A feed showing
`disconnected` or `silent` is the failure mode worth watching for — a dead options
feed makes every quote stale and the gates will correctly refuse to trade.

**Do not run a second process against the same feeds.** A second connection is
refused with 406; the incumbent survives, so the live agent is safe, but any
inspection tool must read the trace rather than open its own stream.

## The panel

```bash
PYTHONPATH=src .venv/bin/python scripts/panel.py --run-dir .run/live --port 3001
```

Equity, P&L against the $100,000 start, session state, cycle count, an equity line
with the starting balance dotted across it, open positions, and the decision log
grouped by cycle — trigger, the agent's reasoning in full, collapsed machine output,
and the outcome.

Read-only and separate from the agent, so it cannot affect a trade. On the server it
binds loopback; reach it through an SSH tunnel rather than opening a port.

## Through the week

- Entries are blocked before 09:45 and after 15:45 ET, and outside the scored window.
- Thursday winds down from **15:00 ET**, not 15:45. The book must reach final
  posture by 16:00 Thursday.
- Do not carry gap risk into Friday: the employment report lands 08:30 ET Friday,
  an hour before a possible 09:30 snapshot.
- Post to X/LinkedIn daily tagging `@lablabai` and `@AlpacaHQ` — a scored criterion
  with two separate $500 prizes, and it costs minutes.

## If something breaks

| Symptom | What it means | Action |
|---|---|---|
| `BLOCKED_LIQUIDITY` repeatedly | spread threshold too tight for live quotes | re-run `calibrate.py`, adjust `risk_params.py` |
| `ERROR: output contract` | model replies malformed past the repair budget | check the provider is up; the chain falls back automatically |
| stream `disconnected` | feed dropped | it reconnects with backoff; if persistent, restart the process |
| `NO_TRADE` every cycle | gates are refusing, or the model is declining | read the `reason` in the trace — a declining model names the gate |
| every cycle `EXECUTED` too fast | debounce or cycle cap misconfigured | 10-minute debounce, 20 cycles/session |

## Submission, Friday before 11:00 ET

Checklist is in `ARCHITECTURE.md` §18. Read the account ID for the form from the
private `ALPACA_ACCOUNT_ID` runtime configuration; do not copy it into source.
