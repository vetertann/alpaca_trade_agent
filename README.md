# Adaptive Alpaca Options Code Agent

An autonomous options trading agent that **writes its own decision programs**. Each
cycle, a model receives a deterministic observation bundle and may emit up to three
Python programs. A program can read chains, run a simulation, and print evidence;
the model then observes that result before writing the next program, staging a
trade, or recording `NO_TRADE`.

Built for the Alpaca AI Trading Agents Hackathon. Scored window: Monday 31 August
09:30 ET through end of day Thursday 3 September 2026.

## What is different about it

Most trading agents call one tool at a time and let a fixed pipeline do the
deciding. Here the decision *is* a program. The model composes twenty-odd
capabilities into whatever analysis the situation needs, and the host — which owns
the credentials and the risk gates — decides whether the result may reach the broker.

```
             preflight bundle (deterministic, from warm local state)
                              │
                              ▼
                 model writes program (up to 3)
                              │
        ┌─────────────────────▼─────────────────────┐
        │  SANDBOX — no credentials, no egress      │
        │  capability stubs block on a pipe         │
        └─────────────────────┬─────────────────────┘
                              │ blocking RPC
        ┌─────────────────────▼─────────────────────┐
        │  HOST — keys, policy verifier, limiter    │
        └─────────────────────┬─────────────────────┘
                              ▼
             official Alpaca CLI / Trading API
```

## Running it

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env      # then fill in the keys
# Install Alpaca CLI v0.0.14 from its official release; deploy.sh does this on the VM.
alpaca version
```

```bash
PYTHONPATH=src .venv/bin/python -m agent.run --profile competition --mode propose --once
```

`--profile` is required and has no default. `--mode propose` stages orders and
prints the gate checklist without submitting; `--mode execute` submits.
`--dev-models` routes to cheap Nebius models for build-and-test iteration.

```bash
.venv/bin/python -m pytest tests/ -q          # 510 passed
```

Deploy the competition agent to the server. The trading process itself listens on
no port; the separately deployed read-only panel is described below.

```bash
./deploy/deploy.sh --profile competition --mode execute
```

The live VPS runs one judged trading process:

| Role | Agent unit | Directory | Risk ceiling | Read-only panel |
|---|---|---|---:|---:|
| Competition, the only judged account | `alpaca-agent.service` | `/opt/alpaca-agent` | 4% robust / scenario ceiling | `:7001` |

There must be only one stream-owning process for the account. Baseline comparisons
run in-process as shadow policies and place no broker orders.

## Design decisions worth naming

**Two-phase execution.** The first `trading.execute(intent)` stages and returns the
rendered gate checklist; nothing is submitted. A later model program can confirm
with one identical call. A second call inside the staging program is host-blocked.
The host materialises the executable order — exact symbols, quantity, limit price —
from quotes it fetches at staging time, so a price computed by generated code can
never ship. The staged intent carries a TTL and a single-use nonce.

**Durable-before-submit execution.** The exact broker request, semantic fingerprint,
and structure-purpose action key are fsynced to `.run/execution.jsonl` before the
official Alpaca CLI is invoked. A timeout is `UNKNOWN`, never rejection: reconciliation
looks up the same `client_order_id`, requires an age floor and repeated 404 before an
exact retry, and freezes new entries while exits continue. Repriced exits dedupe by
structure and purpose, so a changed limit cannot create a second close.

Broker snapshots are then reconciled monotonically into fill deltas, so partial
fills, open risk, realised losses, structure membership, pending exits, and restart
recovery share one source of truth. Tier 0 submits closing orders directly when
deterministic profit targets, short-premium loss stops, exact thesis/expiry, or
final-session time stops fire. A model cycle can also close an exact reconciled
structure when current evidence satisfies its written invalidation.

**Restart continuity without stale decisions.** The rolling second/minute series,
trigger counters, campaign starting equity, compact decision history, and a fresh
previous bundle are atomically checkpointed. Stale bundles and trigger baselines are
dropped and rebuilt; staged drafts, generated code objects, and sandbox variables
are deliberately never restored.

**Evidence- and book-sized entries.** The requested risk is only an upper bound.
Three positive measures plus stable cross-measure rank earn the configured robust
ceiling; the supported tier earns 1.5%, two positive but unstable measures earn
0.5%, and weaker evidence earns zero. The host then takes the minimum of that
evidence ceiling, requested risk,
single-position cap, remaining portfolio premium risk, buying power, realised-loss
headroom, and the correlated scenario-risk solver. Economics and every gate are
recomputed at the final quantity. Zero headroom is a clean `NO_TRADE`; there is no
forced one-lot floor.

**Resulting-book scenario gate.** Before staging, the host shocks SPY, QQQ and IWM
together over −1/−0.5/0/+0.5/+1 expected moves and unchanged/+20% IV. Existing
positions start from their executable close value, a candidate starts from its
executable entry value, and observed per-leg half-spreads remain in the scenario
close. The balanced default permits 4.0% of equity on that grid. Chronological
replay still records 1.50% as the smallest historical anchor; the configured value
is an explicit tournament policy, not an estimated optimum. Quantity is
solved exactly across every scenario, including the lower-bound case where a
risk-reducing candidate repairs a book already over the limit. Missing inputs fail
closed. A live breach freezes risk-increasing entries, keeps exits and repairing
entries available, fires an urgent model review, and clears only below a 0.10%
hysteresis band.

**Directional alignment is separate from volatility edge.** The host derives
1/5/15/30/60-minute returns, normalized displacement, path efficiency, position in
the observed session range, and SPY/QQQ/IWM confirmation from streamed equity quote
midpoints. `market.directional_context()` exposes the labelled ingredients;
`risk.direction()` joins them to candidate bias, breakevens, expected-move P&L
scenarios, and the resulting book delta. A direction-led candidate that conflicts
with the observed path is refused. Neutral or insufficient evidence is capped at
0.75% risk; aligned directional exposure is capped at the configured host ceiling
(3% in the balanced default). The same limits apply to
mixed structures, while genuinely volatility-led, near-delta-neutral structures are
controlled by ensemble, scenario and concentration gates instead of a tape cap. This
is a contradiction guard, not a command to chase momentum, and it never labels
midpoint data as volume, order flow, sentiment, or a return forecast.

**Host-resolved contracts.** Every proposed leg is resolved through Alpaca's option
contract endpoint and checked against OCC symbology. A mismatch in underlying,
strike, type, or expiry is refused, as are duplicate legs, unreduced ratios,
multi-expiration structures, and net short-call exposure with unbounded loss.

**No drawdown stop on long premium.** Maximum loss is the premium and is bounded at
entry, so a mark-to-market stop sells the convexity the premium was bought to own.
Short-premium structures, whose loss runs to the spread width, do carry a stop.

**Zero-bid legs are refused.** A contract at bid 0.00 / ask 0.01 can be bought and
not sold, which turns bounded maximum loss into certain loss.

**Maximum loss follows Alpaca's own margin method** — the universal spread rule:
intrinsic value at every strike present, payoffs netted, worst point taken, per
expiration. Matching their method keeps our risk model aligned with buying power.

**Tiered loop.** Tier 0 consumes four websocket streams and evaluates exits with no
model involvement. Tier 1 tests numeric predicates. Tier 2 — the expensive part —
runs on active-session startup, every 20 minutes while correlated scenario risk is
below the configured build target (3.5% in the balanced default) and operational
capacity remains, four session anchors,
or a live price, volatility, portfolio-P&L, stop-approach, fill, assignment, or
relevant-news event. Tier 0 records normalized structure marks and executable close
values every ten seconds. Ordinary market events are debounced at five minutes;
fills, assignments, the first crossing toward a deterministic stop, and the first
portfolio-scenario breach are urgent.
The model can delegate a durable executable-profit trailing policy for an exact
structure to Tier 0; its high-water mark survives restart, policies may only tighten,
and a confirmed giveback submits the close immediately without another model turn.
Price-dependent immediate actions also carry a host-rechecked executable boundary,
and short-lived durable entry/exit triggers can act between reasoning turns. Spot
invalidations require persistent samples and survive restarts; same-day settlement
requires a separate durable authorization revalidated continuously. The exact
condition and remaining life return in the next observation. Mandatory risk
and time exits remain host-owned and cannot be cancelled by the trigger interface.

**Latest-only multi-turn state.** A cycle may run three model/program rounds. Safe
small Python values persist between those rounds and the next prompt receives one
authoritative manifest of names and types; DataFrames, modules, capability objects,
and oversized objects are dropped explicitly. Previous proposal code and persuasive
reasoning are removed before order confirmation. The rolling market series and
runtime scheduler survive a process restart, but staged drafts and per-cycle sandbox
variables do not.

**Three probability measures, not one.** A single distribution manufactures edge, and
at short horizons the tail dominates the comparison between long gamma and
defined-risk credit. Candidates are priced under an EWMA lognormal, an empirical
block bootstrap, and a Student-t. Agreement supports normal sizing; a positive
median supported by two measures is eligible only at reduced size with the dissent
recorded. Contracts expiring after scoring are marked at Thursday close with their
remaining time value and IV sensitivity; they are never treated as if terminal
payoff arrived early. In live testing a call credit spread scored +0.123 under the lognormal and
−0.028 under the bootstrap—uncertainty that belongs in sizing rather than being
hidden by a single headline number.

**Shadow baselines.** Four fixed policies run inside the same process against the
same live quotes, placing no orders. They exist so the final equity number can be
read against a control rather than in isolation. Entry and live marks cross the
quoted spread; the simulation does not model market depth, additional slippage, or
fees, so a large same-day baseline position is evidence of path exposure rather than
a claim that its complete displayed size was fillable in a real account.

## Telemetry

The durable JSONL trace retains the final 16,000 characters of each program's stdout,
the full capability-call list, generated source and hash, verification, fills, and
outcome. The panel collapses program source visually but does not remove it.

The agent also emits OpenTelemetry GenAI spans to an OTel collector — `invoke_agent` per cycle,
`chat` per model call with token and reasoning usage, `execute_tool` per capability
call, linked by `gen_ai.tool.call.id`. Set `COLLECTOR_HOST`; content capture is
opt-in via `OTEL_CAPTURE_CONTENT`.

## Disclosures

**Pre-event work.** Repository scaffolding, the Python environment, and vendored
upstream reference material (`vendor/`, gitignored) were prepared before the
competition window opened. All agent source in `src/` was written during the event.

**Third-party packages.** `alpaca-py`, `anthropic`, `openai`, `httpx`,
`websockets`, `msgpack`, `numpy`, `pandas`, `scipy`, `python-dotenv`, `pytest`,
`opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-grpc`.

**Model providers.** Anthropic (`claude-opus-5`), OpenAI (`gpt-5.6-sol`, `gpt-5.5`, `gpt-5.4`), and
Nebius AI Studio (`openai/gpt-oss-120b`, `moonshotai/Kimi-K3`,
`Qwen/Qwen3.5-397B-A17B`) behind one shim, selected per role with fallback chains.
Featherless AI is wired for the news-triage role.

## Repository map

```
src/agent/
  panel/index.html   the panel, one file, no framework
  config.py          profiles, credential aliases, window guards
  types.py           TradeIntent -> VerifiedTradeIntent, gates, thesis
  run.py             the runner: tiers, triggers, cycles
  quant/             Black-Scholes, structure economics, vol, measures, candidates
  host/              REST, streams, contracts, durable ledger, gates, execution, trace
  sandbox/           pipe-RPC sandbox and the child runtime
  brain/             providers, prompt assembly, preflight, loop, shadow
  prompts/           core.md, domain.md, pretrade.md
scripts/
  panel.py           read-only operator panel over the JSONL trace
  fill_probe.py      isolated multi-leg fill-denomination rehearsal
  recovery_probe.py  fault-injected timeout-after-accept rehearsal
  portfolio_risk_replay.py  chronological fill replay and scenario-cap calibration
  warmup_check.py    GO/NO-GO protocol for 09:30-09:45 ET
  calibrate.py       live spread and volatility-state measurement
  estimate_cost.py   token and cost projection from measured usage
deploy/              systemd unit, provisioning, deploy script
tests/               510 passing tests
```

Design detail lives in `ARCHITECTURE.md`. Operating procedure lives in `RUNBOOK.md`.

## Disclaimer

Paper trading only. Nothing here is investment advice. Options trading carries
substantial risk of loss.
