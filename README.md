# Adaptive Alpaca Options Code Agent

An autonomous options trading agent that **writes its own decision programs**. Each
cycle, a model receives a deterministic observation bundle and emits one Python
program that runs to completion — reading chains, testing a hypothesis, ranking
candidates, and submitting an order — with no model turn between steps.

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
                    model writes one program
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
                    Alpaca MCP / Trading API
```

## Running it

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
cp .env.example .env      # then fill in the keys
```

```bash
PYTHONPATH=src .venv/bin/python -m agent.run --profile dev --mode propose --dev-models --once
```

`--profile` is required and has no default. `--mode propose` stages orders and
prints the gate checklist without submitting; `--mode execute` submits.
`--dev-models` routes to cheap Nebius models for build-and-test iteration.

```bash
.venv/bin/python -m pytest tests/ -q          # 267 passed
```

Deploy to a server (the agent listens on no port; nothing is opened):

```bash
./deploy/deploy.sh --profile competition --mode execute
```

## Design decisions worth naming

**Two-phase execution.** The first `trading.execute(intent)` stages and returns the
rendered gate checklist; nothing is submitted. A later model program can confirm
with one identical call. A second call inside the staging program is host-blocked.
The host materialises the executable order — exact symbols, quantity, limit price —
from quotes it fetches at staging time, so a price computed by generated code can
never ship. The staged intent carries a TTL and a single-use nonce.

**Durable execution state.** Submitted entries and exits are appended to
`.run/execution.jsonl`. Broker order snapshots are reconciled monotonically into
fill deltas, so partial fills, open risk, realised losses, structure membership,
pending exits, and restart recovery share one source of truth. Tier 0 submits
closing orders directly when deterministic profit, short-premium, or final-session
time stops fire.

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
runs only when a predicate fires: four session anchors plus move, volatility, fill,
assignment, and news triggers, debounced at ten minutes.

**Three probability measures, not one.** A single distribution manufactures edge, and
at zero-to-five days the tail dominates the comparison between long gamma and
defined-risk credit. Candidates are priced under an EWMA lognormal, an empirical
block bootstrap, and a Student-t, and only what survives all three is traded. In
live testing a call credit spread scored +0.123 under the lognormal and −0.028 under
the bootstrap — an artifact a single measure would have sold as edge.

**Shadow baselines.** Four fixed policies run inside the same process against the
same live quotes, placing no orders. They exist so the final equity number can be
read against a control rather than in isolation.

## Telemetry

Emits OpenTelemetry GenAI spans to an OTel collector — `invoke_agent` per cycle,
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

**Model providers.** Anthropic (`claude-opus-5`), OpenAI (`gpt-5.5`, `gpt-5.4`), and
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
  fill_probe.py      settles multi-leg fill denomination on the dev account
  warmup_check.py    GO/NO-GO protocol for 09:30-09:45 ET
  calibrate.py       live spread and volatility-state measurement
  estimate_cost.py   token and cost projection from measured usage
deploy/              systemd unit, provisioning, deploy script
tests/               267 passing tests
```

Design detail lives in `ARCHITECTURE.md`. Operating procedure lives in `RUNBOOK.md`.

## Disclaimer

Paper trading only. Nothing here is investment advice. Options trading carries
substantial risk of loss.
