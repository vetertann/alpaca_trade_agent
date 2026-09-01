# Competition operations runbook

Scored window: **Monday 31 August, 09:30 ET through Thursday 3 September, 16:00
ET**. The first-session procedures are retained as evidence; the later sections are
the current operating instructions.

## Monday rehearsal status

The first-session rehearsal and live session are complete. The system has now
observed live equity and option quotes, populated and restored its rolling series,
submitted and cancelled orders, filled multi-leg entries and an exit, reconciled
those fills after restarts, and enforced the 15:45 ET entry cutoff.

The fill-denomination question is resolved by the durable broker ledger: across
live verticals and condors, parent `filled_qty` equalled the submitted spread
quantity, including the four-lot exit. Realised P&L
therefore correctly treats parent fill quantity as spreads, not aggregate leg
contracts.

The procedures below remain the reproducible first-session protocol. Do not rerun a
fill or recovery probe merely because the service restarted. Those scripts are
historical rehearsal tools, not part of the production startup path; rerun one only
when the relevant execution semantics changed.

## Before the open

```bash
cd /Users/ivan/Documents/Hackatons/Alpaca
set -a; . ./.env; set +a
.venv/bin/python -m pytest tests/ -q          # expect 414 passed
alpaca version                                # v0.0.14; /usr/local/bin/alpaca on VM
```

Confirm account identity and current state. After Monday, orders and positions are
expected; zero is no longer a valid success condition:

```bash
PYTHONPATH=src .venv/bin/python -c "
from agent.config import load_env, profile; from agent.host.rest import Rest
load_env(); r = Rest(profile('competition'), execution_transport='cli')
a = r.account()
print('account', a['account_number'], '| equity', a['equity'],
      '| positions', len(r.positions()), '| open orders', len(r.orders('open')))"
```

## Completed execution protocol

The live first-session checks are complete and must not be repeated on an ordinary
restart. They established quote delivery, submit/cancel, multi-leg entry and exit
fills, restart reconciliation, and parent fill denomination. Fault-injected tests
cover timeout-after-accept, partial-fill cancellation, and exact retry after a
confirmed absence. Treat these as recorded evidence, not recurring production
startup actions; rerun a broker-mutating probe only after the relevant Alpaca
semantics or execution code changes.

## 09:35 ET — calibrate, before the first entry

Closing quotes are systematically wider than intraday, so every parameter measured
on Saturday is an upper bound.

```bash
PYTHONPATH=src .venv/bin/python scripts/calibrate.py --profile competition --apply
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

The chronological fill replay found **1.50% of equity** as the smallest historical
admission anchor, but the deployed four-session contest policy is deliberately
**4.0%**. The larger number is a forward policy choice, not a backtest optimum: it
lets qualified P&L matter while remaining far below the former 10% comparator. Do
not change it intraday because the current book is breached or because a rejected
trade later would have made money.

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

The deployed topology is:

| Account | Agent | Run directory | Panel |
|---|---|---|---|
| competition | `alpaca-agent.service` | `/opt/alpaca-agent/.run` | TCP 3001 |

Run exactly one process for the account. Alpaca refuses a second connection to the
same feed/account with 406 and the incumbent wins. Shadow baselines remain inside
that process and do not place orders.

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

Left column: equity, P&L against the $100,000 start, session state, cycle count, an
equity line with the starting balance dotted across it, normalized structures, the
shadow baselines ranked by return, and model usage with Anthropic cost. In the
portfolio table, `VALUE` is signed entry cash flow, `BROKER` is Alpaca's mark-based
unrealized P&L, `SELL NOW` is P&L at immediately executable closing sides, and
`AUTO EXIT` is the host-managed hard or adaptive profit threshold. These columns
are deliberately different; a midpoint mark is not assumed sellable.

The compact `TRACE PROOF` line is derived from the append-only trace: cycles,
no-trades, unique submitted and filled orders, reconciliations, host-fired exits,
current open executable P&L, and gate refusals grouped by reason. Its scope is the
current trace file; it is evidence of the running system, not a reconstructed claim
about records that are absent from that file.

Active one-shot actions and their recent terminal outcomes appear as compact
`HOST TRIGGER` rows below the portfolio, with the exact condition, labelled state,
failed gates, last host observation and seconds remaining. `blocked_risk` means the
price condition crossed but a durable risk gate failed admission; the authorization
is terminal and does not retry every second. `waiting_data` instead means quote
validity or spread quality was temporarily unusable: the rule stays active and the
host retries after five seconds. No more than three blocked triggers per ET session
grant an urgent reconsideration, and those reviews still consume the ordinary
session cycle budget. A
price-sensitive immediate order should show a fresh-price boundary in its trace;
hard invalidation and time exits are intentionally unconditional. Trigger state is
fsynced in `.run/action_triggers.jsonl`, survives restart, and expires explicitly.
Removing a discretionary trigger cannot remove a mandatory exit or cancel a broker
order that has already fired.

The execution-control strip also shows correlated portfolio scenario loss against
the 4.0% host cap. `RISK-REDUCING ENTRIES ONLY` means the live book is over that
cap: exits remain enabled, ordinary entries are refused, and only an exact candidate
quantity that repairs every binding scenario may stage. Do not clear state or raise
the cap to remove this message; it clears automatically only after loss falls below
the hysteresis floor.

Right column: the decision log grouped by cycle — trigger, the agent's reasoning in
full, collapsed machine output, and the outcome. It runs to the bottom of the
viewport, so a taller screen shows more cycles rather than more whitespace.

The baselines table is the comparison the evidence rests on: four fixed policies
reading the same live quotes, placing no orders, so the final equity number can be
read against a control instead of in isolation.

Each policy re-enters through the week — it settles at expiry against the underlying
and opens again the next session — so `trades` should climb across the window. A
baseline stuck at one trade after Tuesday means settlement is not firing and the
comparison is measuring a single Monday position.

Read-only and separate from the agent, so it cannot affect a trade. In the current
paper-demo deployment the panel binds `0.0.0.0` on port 3001, and UFW permits that
port. It is unauthenticated and must
never expose credentials, raw environment values, or mutation endpoints; every
`POST` returns 405. For a non-demo deployment, bind loopback and use an SSH tunnel.

## Through the week

- Entries are blocked before 09:45 and after 15:45 ET, and outside the scored window.
- Thursday winds down from **15:00 ET**, not 15:45. Expiring structures flatten;
  later-dated contracts may remain when their marked exposure is the deliberate
  final-equity posture. Confirm that every such candidate used `vol.measures_for`.
- On every expiry day, ordinary liquidation begins at **15:15 ET**. Holding beyond
  it requires a durable settlement authorization that currently passes usable-quote,
  finite-risk, scenario, buying-power and short-strike-distance checks. The host
  performs a named final pre-broker-risk review at 15:28 and keeps revalidating.
- The FAQ's equity mark is EOD Thursday September 3; the formal measurement window
  ends Friday September 4 at 09:30 ET. Valuation and eligibility keep them distinct.
- Post to X/LinkedIn daily tagging `@lablabai` and `@AlpacaHQ` — a scored criterion
  with two separate $500 prizes, and it costs minutes.
- New entries require same-cycle `market.directional_context` and
  `risk.direction` evidence for the exact candidate. A conflicted direction-led
  structure is rejected; neutral or insufficient evidence is capped at 0.75% only
  for direction-led or mixed structures. Genuinely volatility-led structures use
  ensemble and resulting-book controls instead.
- The model's requested risk is never authoritative. Three positive measures plus
  stable rank earn 4%; three positive but unstable or two positive and stable earn
  1.5%; two positive and unstable earn 0.5%; weaker evidence earns zero.
- Every staged and confirmed entry must also keep the resulting correlated
  SPY/QQQ/IWM book inside the 4.0% executable scenario-loss cap. Confirmation uses
  fresh quotes and may preserve, reduce, or block the reviewed quantity, never
  increase it.

## If something breaks

| Symptom | What it means | Action |
|---|---|---|
| `BLOCKED_LIQUIDITY` repeatedly | spread threshold too tight for live quotes | re-run `calibrate.py`, adjust `risk_params.py` |
| `ERROR: output contract` | model replies malformed past the repair budget | check the provider is up; the chain falls back automatically |
| stream `disconnected` | feed dropped | it reconnects with backoff; if persistent, restart the process |
| `ENTRIES FROZEN` | an ambiguous submission is reconciling | leave the process running; exits remain enabled and successful reconciliation clears it automatically |
| `ENTRIES LATCHED` | broker order semantics disagree with the durable request, or an unknown prefixed order exists | inspect the panel and broker order; do not clear the ledger or start another process |
| `RISK-REDUCING ENTRIES ONLY` | current executable correlated stress exceeds 4.0% of equity | keep the service running; exits still work, an urgent review fires, and the restriction clears below the hysteresis floor |
| `portfolio_scenario: incomplete` | a spot, leg quote, contract field, or implied volatility needed by stress is unavailable | do not treat it as zero; the entry gate fails closed until fresh complete inputs arrive |
| `NO_TRADE` every cycle | gates are refusing, or the model is declining | read the `reason` in the trace — a declining model names the gate |
| `needs_evidence: market.directional_context` | generated program omitted the exact-underlying direction read | allow the repair round; the host has not staged or submitted anything |
| `needs_revision: direction-led candidate conflicts` | volatility edge and observed price direction disagree | choose an aligned/volatility-led structure or decline; do not bypass the gate |
| every cycle `EXECUTED` too fast | debounce or cycle cap misconfigured | 5-minute debounce, 24 cycles/session |

## Account identifiers

Alpaca names one account two ways, and only one of them appears in the dashboard:

| Field | Form | Where you see it |
|---|---|---|
| `account_number` | paper account number returned by Alpaca | the Alpaca web dashboard |
| `id` | UUID returned by Alpaca | `/v2/account` only |

Both come from the same `/v2/account` response and refer to the same account.
`ALPACA_ACCOUNT_ID` accepts either.

For the submission form, give **both** — the number so a human can match it to the
dashboard, the UUID so their tooling can. There is no cost to supplying both and a
real cost to guessing which one they meant.

```bash
PYTHONPATH=src .venv/bin/python -c "
from agent.config import load_env, profile
from agent.host.rest import Rest
load_env(); a = Rest(profile('competition'), execution_transport='cli').account()
print('account_number:', a['account_number']); print('id:', a['id'])"
```

## Submission, Friday before 11:00 ET

Checklist is in `ARCHITECTURE.md` §18. Read the account ID for the form from the
private `ALPACA_ACCOUNT_ID` runtime configuration; do not copy it into source.
