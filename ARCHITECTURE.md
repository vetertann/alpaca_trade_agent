# Architecture — Adaptive Alpaca Options Code Agent

Revision 4, 2026-08-29. Adds the decision-program model, runtime state, failure policy, and the trigger architecture.

---

## 1. Operating constraints

Verified against the live account, the Alpaca API, and Alpaca's published guidelines.

### The clock

The scoring window is shorter than the hackathon window.

```
Sat Aug 29   build                              (market closed)
Sun Aug 30   build                              (market closed)
Mon Aug 31   09:30–16:00 ET   SCORED   agent must be live at the open
Tue Sep 01   09:30–16:00 ET   SCORED
Wed Sep 02   09:30–16:00 ET   SCORED
Thu Sep 03   09:30–16:00 ET   SCORED   ← last actionable moment, 16:00 ET
Fri Sep 04   09:30 ET equity snapshot · 11:00 ET submission deadline
```

Alpaca's guidance carries an internal contradiction: the measurement window is stated as Monday 09:30 through Friday 09:30, and the measured value is stated as **"the portfolio's total equity as of EOD Thursday Sep 3rd."** Friday's snapshot falls at the opening bell, so no Friday trading is possible in either reading.

**Operative rule: four sessions, ending Thursday September 3 at 16:00 ET.** The portfolio must be at its target posture by Thursday's close, with overnight gap exposure into Friday's open minimised so the two readings agree.

Alpaca states that exercises and assignments for contracts expiring September 3 are reflected in the Thursday EOD value. That mechanism is not something the strategy depends on: non-trade activities post the following day, assignment can occur after the close, and Alpaca begins its own expiry risk management around 15:30 ET.

**Design rule: expiry processing is not part of the strategy.** Anything that matters to the score is explicitly closed, or intentionally left in a known marked state, before the cutoff. Score should never rest on post-close mechanics we do not control.

Build time before going live: this weekend.

### Accounts

Alpaca requires the scored account to be separate from the account used for development: *"An account used for testing should not be used for the official measurement."*

Separate competition and development paper accounts are configured at runtime.

| Role | Account ID | Number | Use |
|---|---|---|---|
| **Competition** | `ALPACA_ACCOUNT_ID` | Competition credentials | No test orders. The identifier remains outside source control. |
| **Development** | `DEV_ALPACA_ACCOUNT_ID` | Development credentials | All testing, calibration, and order rehearsals. |

### Credentials

Held in `.env`, which is excluded from the repository. Names only appear below; values never enter this document, the trace, or any log.

| Variable | Purpose |
|---|---|
| `ALPACA_ACCOUNT_ID` / `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Competition account identifier and credentials. |
| `DEV_ALPACA_ACCOUNT_ID` / `DEV_ALPACA_API_KEY` / `DEV_ALPACA_SECRET_KEY` | Development account identifier and credentials. |
| `NEBIUS_API_KEY` | Nebius AI Studio. |
| `FEATHERLESS_API_KEY` | Featherless AI, added when the second provider lands. |

`.env.example` carries the full shape with empty values and is committed.

Rules the host enforces on startup:

- The active profile is selected **explicitly**; there is no default. A run that does not name `competition` or `dev` refuses to start.
- Each profile asserts that Alpaca returns the account ID configured for that profile.
- The `competition` profile also asserts that the clock is inside the scored window before every order.
- Both key prefixes are checked for `PK`, and the resolved trading endpoint must be `https://paper-api.alpaca.markets`.
- Credentials live in the host process only. The sandbox holds none.

### What Alpaca provides

| Capability | Status |
|---|---|
| Option chains and latest quotes | Real-time on the free Basic plan |
| Basic plan feed | **Indicative options feed**, live but not full OPRA. No OPRA access is granted for the event |
| Option historical bars and trades | Available from Feb 2024, excluding the most recent 15 minutes on Basic |
| **Option historical quotes** | **No endpoint accepts a time range** |
| Greeks and implied volatility | **Available** on the indicative feed for contracts with a valid two-sided quote. Absent exactly where the quote is unusable — measured 8/8 present near the money, 0/3 on zero-bid strikes |
| Expired contract universe | Queryable via `status=inactive` |
| Earnings calendar | Absent from the Alpaca API |
| Options session | Regular hours only, 09:30–16:00 ET |
| Rate limit | 200 requests/min for market data and 200/min for trading, independently |
| Equity real-time coverage | IEX exchange only on Basic; fills are priced by Alpaca against full market data |
| Stream subscriptions | 30 equity symbols, 200 option quotes |

Consequences: locally computed Black-Scholes is our canonical calculation, with Alpaca's values available as a cross-check wherever a contract quotes two-sided — which is every contract we would trade; liquidity and fill economics are measurable live and unmeasurable historically; event-volatility strategies would need an external feed into a window that sits between reporting seasons.

### Rulings that bind the design

From Alpaca's official guidelines and FAQ, which supersede the kickoff chat.

| Ruling | Consequence |
|---|---|
| **All trading in the competition period happens inside one paper account** | No parallel live baselines. Comparison runs in shadow mode. |
| P&L is **total account equity**, not cash balance | Open positions mark into the score. |
| **No risk-adjusted metrics.** No Sharpe, Sortino, or drawdown | Terminal equity is the only performance number. |
| Judged on P&L **and** the *creativity, autonomy, and robustness of the agent trading workflow*; winners are not selected on P&L alone | The workflow is a first-class deliverable. |
| **A user interface is not required.** A repository suffices for an agent that only places orders | No dashboard app. |
| **MCP or CLI preferred.** Using an SDK directly requires a clear written justification, with official SDKs prioritised | Execution routes through MCP or CLI. |
| MCP option orders support **market, limit, stop, and stop-limit**; trailing stops are equities-only | Stop-based exits are available. |
| No restrictions on options strategies | 0DTE and every defined-risk structure permitted. |
| Backtests and simulated shocks are **encouraged** in the repo and write-up as evidence of guardrails | Shock simulation is a scored artifact. |
| External data permitted; scipy and statsmodels welcome, named in the README | |
| Pre-event setup permitted and **must be disclosed** in the README | Our vendored repos and environment prep get disclosed. |
| Repository may stay private during the event | |
| No restrictions on model provider or hosting | Nebius and Featherless both fine. |

---

## 2. Objective

Four sessions of dispersion overwhelm the edge available from strategy selection, and terminal equity is the only performance number recorded. Alpaca states twice that winners are not chosen on P&L alone, and names *creativity, autonomy, and robustness of the agent trading workflow* as the co-equal pillar.

The objective resolves to:

> Maximise terminal account equity through adaptive selection among bounded-risk option structures, while making the autonomy and robustness of that selection inspectable.

Operating rules that follow:

- Maximum loss is known and bounded before any order is sent.
- Capital is deployed early. An agent that holds cash through four sessions scores the starting balance.
- Convexity is one candidate family among several, evaluated on evidence rather than assumed as the default posture.
- Contract tenor matches the window: four days is most of the life of the contracts traded.
- The book reaches its target posture by Thursday 16:00 ET.

Family selection happens from evidence gathered at decision time. The system chooses among directional spreads, long convexity, defined-risk premium selling, relative value, and no trade.

---

## 3. Core architecture

An LLM writes a Python program that carries a decision from observation through to a submitted order. The program performs many dependent calls, loops, joins, and calculations in one pass, with no LLM turn between results. The host holds credentials and enforces every invariant, which is what makes it safe to let generated code reach the broker.

```text
                         LLM
                          │
                 writes a decision program
                          │
                          ▼
          ┌───────────────────────────────┐
          │      SANDBOX (child proc)     │
          │      no network egress        │
          │      no credentials           │
          │  capability stubs over a pipe │
          └───────────────┬───────────────┘
                          │  blocking RPC
                          ▼
          ┌───────────────────────────────┐
          │      HOST (parent proc)       │
          │  credentials · policy verifier│
          │  thesis store · trade stream  │
          │  rate limiting · audit log    │
          └───────────────┬───────────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        LIVE ADAPTER            REPLAY ADAPTER
        Alpaca MCP / CLI        frozen bars + trades
```

The child blocks on each capability call. The host performs the request, runs the policy verifier, and returns the result. Credentials never enter the sandbox, and generated code reaches the broker only through verified capabilities.

The transport is a pipe. Replacing it with HTTPS later leaves generated-code semantics unchanged.

**Execution path.** Orders and account reads go through the **Alpaca MCP server**, which covers contracts, chains, quotes, and single- and multi-leg option orders. The CLI is installed alongside for inspection and for a human-readable command log. `alpaca-py` is used only where MCP lacks a capability, and every such use is named and justified in the README as the guidelines require.

**Run modes.** `propose` halts after the program returns its intended orders. `execute` submits them. Development runs use `propose`; the scored window uses `execute`.

### Generated-code contract

- Fixed preamble with capability namespaces plus `numpy`, `pandas`, `scipy`, `math`, `statistics` pre-imported.
- The preflight observation bundle is preloaded as `obs`. The program starts from a described world rather than fetching one.
- Programs return a structured result object; free-form output goes to the trace.
- Execution failures return the traceback to the model for repair, with a bounded retry count.
- Every reply is a single `{thought, code}` object. `thought` is one or two sentences of plan; `code` is executable Python only, no prose, no fences. Malformed replies get a typed repair message, up to three attempts.
- The model may run up to three analysis rounds per cycle. **The namespace persists across rounds**, so round two reuses what round one loaded rather than re-fetching it. The round budget is enforced by the host.
- Target one or two rounds. Round one batches every obvious computation into a single block; a second round is for a specific missing piece that round one revealed, not for exploring the same ground again.
- Known error classes return a targeted hint alongside the traceback rather than the traceback alone.
- Capability namespaces are read-only bindings; rebinding is rejected.
- Every program is stored verbatim, hashed, and linked to the orders it produced.

The repair loop is mandatory. Generated code fails routinely, and unhandled failure makes the code agent less reliable than the stepwise approach it replaces.

---

## 4. Capability API

About twenty-five functions, each tested. The model composes the rest in the program itself.

### `TradeIntent` → `VerifiedTradeIntent`

Generated code proposes; the host materialises. The executable limit price and final quantity are never authoritative fields arriving from a program.

```text
TradeIntent                          written by the model
  underlying · family · leg geometry (strikes, types, ratios)
  expiry · desired risk budget · thesis_id
        │
        ▼  host refreshes quotes, contracts, and account state
VerifiedTradeIntent                  materialised by the host
  exact contract symbols · qty · limit price · computed max loss
  quote snapshot hash · TTL · nonce
        │
        ▼  consume once
     execute
```

The model may compute whatever it likes and rank on its own numbers. What ships is built from quotes fetched at materialisation time.

- **TTL.** A `VerifiedTradeIntent` expires. Confirmation after expiry re-materialises from fresh quotes rather than submitting a stale price.
- **Nonce, consumed once.** A confirmed intent cannot be replayed, which composes with the deterministic `client_order_id` to make double-submission impossible from either direction.
- **Snapshot hash.** The quotes that priced the order are recorded with it, so the trace shows what the price was built from.

This closes a gap that two-phase staging otherwise opens: without host materialisation, the price staged at 11:00:04 could still be submitted at 11:00:41 against a market that has moved.

Geometry stays with the model, pricing stays with the host.

### Layer 1 — primitives

Identical signatures in live and replay.

```python
market.bars(symbols, timeframe, start, end)
market.snapshot(symbols)
market.latest_quote(symbols)
market.calendar(start, end)
market.news(symbols, limit)

options.contracts(underlying, expiry_range, strike_range, type=None, status="active")
options.chain(underlying, expiry_range)          # contracts joined to live quotes
options.bars(symbols, timeframe, start, end)
options.latest_quote(symbols)

account.state()                                   # account + positions + open orders
orders.status(order_id)
```

### Layer 2 — domain and policy

```python
vol.realized(symbol, window)                      # EWMA and close-to-close
vol.implied(contract, spot, rate)                 # local Black-Scholes
vol.expected_move(chain, dte)                     # from the ATM straddle
vol.state(symbol)                                 # IV against realized, percentile

options.greeks(contract, spot, rate)              # local, all five
options.payoff(structure, price_grid)             # piecewise terminal payoff
options.tradeable_chain(underlying, filters)      # liquidity-gated chain
options.enumerate(chain, families, filters)       # candidate structures
options.evaluate(structure, measure)              # normalized economics

oi_gamma.profile(chain, spot)                     # OI-weighted gamma per strike
oi_gamma.flip_point(chain, spot)                  # sign change under a stated convention
oi_gamma.concentration(chain, spot)               # heaviest OI-weighted gamma near spot

risk.max_loss(structure_or_portfolio)             # Alpaca universal spread rule
risk.budget_check(structure, portfolio, budget)
risk.exposure(portfolio)                          # net delta, gamma, vega, notional

trading.preview(structure)
trading.execute(structure)
trading.close(position_or_structure)
trading.reconcile()

thesis.open(hypothesis, structure, exits)         # exits include price, time, and news invalidation
thesis.list(status="open")
thesis.update(thesis_id, note, status)
```

The `oi_gamma` namespace is named for what it measures. Open interest records how many contracts exist at a strike; it does not record who is long and who is short. Converting it to dealer positioning requires an assumption — conventionally that dealers are short calls and long puts against retail flow — and that assumption is a heuristic, not an observation. The functions therefore expose an OI-weighted gamma profile with the sign convention stated as a parameter, and the write-up says so. Calling it dealer gamma would claim knowledge of positioning we do not have.

`risk.max_loss` implements Alpaca's own maintenance-margin method: intrinsic value at every strike present in the structure, payoffs netted at each point, worst point taken, evaluated per expiration with the largest requirement across expirations. Matching Alpaca's method keeps the agent's risk model aligned with actual buying power and prevents rejection at submission.

`trading.execute` is **two-phase**. The first call stages the order and returns the rendered gate checklist without submitting anything. The pre-trade skill is injected into the next observation, and the model must call `trading.execute` again with an identical structure to confirm. Calling it with different arguments stages a new draft instead.

Staging costs one cheap round trip and only when actually trading. What it buys is a second look at precisely the moment that matters, with the gate results visible rather than assumed.

On confirmation the call runs the full deterministic sequence: read account state, verify contracts, confirm two-sided quotes inside the spread limit, compute bounded max loss, enforce the risk budget, construct position intents, submit through MCP with a deterministic `client_order_id`, manage the fill, confirm, reconcile. Mandatory behaviour lives here rather than in the prompt.

### Helper classes

Every capability falls into one of three classes, and the name says which.

| Class | Property | Examples |
|---|---|---|
| **Read / normalize** | Deterministic, no side effect, safe to call freely | `options.chain`, `vol.state`, `gamma.exposure` |
| **Pure selector** | Operates on already-retrieved data; succeeds only on an unambiguous result, otherwise returns `AMBIGUOUS` or `NOT_FOUND` | `options.enumerate`, `options.evaluate` |
| **Guarded action** | Touches policy or the broker; suffixed `_guarded` where it wraps a raw capability; two-stage where it moves money | `trading.execute`, `trading.close_guarded` |

A guarded action never silently claims success, never invents a capability it lacks, and never picks arbitrarily among several valid options. When the tool surface cannot satisfy it, it returns a stated limitation and the cycle records that outcome.

### Layer 3 — the agent's own extensions

Two capabilities exist so the agent can test its own reasoning and grow its own toolkit.

```python
replay.window(start, end)                         # returns a replay-backed capability set
learned.save(name, source)                        # persist a helper for later programs
learned.list()
```

`replay.window` hands the program a capability set backed by frozen bars and trades over a past interval. Signatures match live where the data supports it, and **every replay result is wrapped in a provenance envelope** so identical signatures never disguise a different epistemic status:

```json
{ "value": 2.41, "provenance": "MODELED", "source": "spread_haircut_v2", "as_of": "2026-07-14T15:30:00Z" }
```

`provenance` is one of `MEASURED`, `MODELED`, or `UNAVAILABLE`. Bars and trades come back `MEASURED`. Anything derived from a modelled spread comes back `MODELED` with the model named.

**`latest_quote` is not implemented in replay.** Historical option quotes do not exist, so a function claiming to return one would be fabricating its central value. It returns `UNAVAILABLE`, and a program needing entry economics over history must ask for a modelled price explicitly and receive it labelled as such.

The agent uses replay to measure a hypothesis before trading it, which turns falsification into a measurement — while keeping visible which parts of that measurement are observations and which are assumptions.

`learned.save` writes a helper into a `learned/` module that subsequent programs import. Functions the agent writes for itself accumulate across the window, and the resulting diff is a record of the system extending its own capability API.

Self-modification during a scored window is the single feature most able to undermine everything else in this document, so it is constrained to **pure analysis**:

- Static AST check before installation. No imports beyond an allowlist of numerical packages, no I/O, no attribute access into capability namespaces.
- **No capability or action calls.** A saved helper cannot trade, cannot read the broker, cannot touch the thesis store, and cannot alter a risk parameter or a gate.
- The helper must execute against a fixture and return a finite result before it is promoted.
- Once installed it is immutable and content-hashed. A revision is a new name, so a program's behaviour is always attributable to a specific version.

The agent extends its analytical toolkit. It cannot reach the safety architecture, which is what makes the feature safe to run unattended for four days.

---

## 5. Data plane

Two ways to obtain data, with separate budgets. Getting the split right is what keeps the watcher continuous and the request budget idle.

### Budget on the Basic plan

| Resource | Limit |
|---|---|
| **Equity** stream subscriptions | 30 symbols, JSON encoding |
| **Option** stream subscriptions | 200 quotes, MessagePack encoding |
| Market data requests | 200 / min |
| Trading requests | 200 / min |
| Historical data | latest **15 minutes unavailable** |
| Equity real-time coverage | IEX exchange only |
| Option real-time coverage | Indicative feed |

The two request budgets are independent: reading market data does not consume the allowance for placing orders. Star subscription is rejected for option quotes, so every contract watched is named explicitly.

### Subscribed

Three streams, all push, none consuming the request budget.

- **Order events** — `wss://paper-api.alpaca.markets/stream`, `trade_updates`. Fills, partial fills, cancels, rejections. Removes any need to poll for order status.
- **Prices** — equities and options.
- **News** — headlines as published, feeding the triage lanes.

Equity allocation, 30 available:

```
SPY, QQQ, IWM                            3   traded underlyings
NVDA AAPL MSFT TSLA AMZN GOOGL META      7   index weight, and where news lands
VXX                                      1   volatility proxy
held or reserved                        ~19  kept free
```

Option allocation, 200 available:

```
legs of open positions                  ~12  never evicted
SPY  ±7 strikes × calls+puts × 2 expiries 60
QQQ  ±7 strikes × calls+puts × 2 expiries 60
                                        ───
                                        132
```

SPY strikes are spaced one dollar apart near the money, so ±7 spans a fourteen-dollar band around spot, wider than a normal session's range. Widening to ±10 costs 168 and still fits.

Streaming the candidate window rather than fetching it means current prices for every plausible candidate are already in memory when a cycle begins. Preflight assembles the observation bundle from local state, with no network round trip on the critical path.

### Polled

| What | Interval | Cost |
|---|---|---|
| Account, positions, buying power | 10 s | 6 req/min |
| Order status reconciliation backup | 10 s | 6 req/min |
| Contract universe per underlying | **once per session** | ~5 requests |
| Historical bars for volatility calibration | startup, then hourly | negligible |
| Chain detail outside the streamed window | inside a decision only | burst of 10–30 |

Steady state runs near 12 requests per minute against a limit of 200. Five decision cycles in an hour, each bursting thirty requests, adds about 2.5 per minute on average.

Option prices are read through `quotes/latest` with explicitly named symbols rather than through `snapshots`. Snapshots paginate in symbol order, so a request can silently return contracts from an unintended expiry; naming symbols removes the failure mode.

Contract listings change overnight rather than intraday, so the universe is fetched once each morning and cached for the session. Re-polling it is the easiest available way to waste the budget.

The host enforces its own token-bucket limiter, so generated code cannot exceed the allowance regardless of how it loops.

### Rolling history

Historical data excludes the most recent fifteen minutes, so the question a decision cycle most needs to answer — what has moved since the last cycle — cannot be served from the historical endpoints for the interval that matters.

The watcher therefore maintains its own rolling series in memory, accumulated from the price stream: per subscribed symbol, a bounded deque of one-second and one-minute aggregates covering the session. This is the source for the `diff` block in the observation bundle and for every threshold predicate. Historical endpoints are used for calibration over days and weeks, never for recent context.

The series is checkpointed to disk on each update so a restart mid-session recovers its recent window rather than starting blind.

### Window management

Spot drifts, so a strike window centred at the open watches the wrong contracts by the afternoon.

- The watcher re-centres the option window when spot leaves the middle third of the current band, and when an expiry rolls.
- Re-centring is subscribe and unsubscribe traffic on the open connection, costing nothing against the request budget.
- Eviction order as the cap approaches: the second expiry first, then the outermost strikes. Legs of open positions are never evicted.

### Measured limits

Tested against the development account on 2026-08-29. These are observations, not documentation.

| Test | Result |
|---|---|
| Equity subscriptions | 30 accepted, **31 rejected** with `405 symbol limit exceeded` |
| Option subscriptions | 200 accepted, **201 rejected** with `405 symbol limit exceeded` |
| Four streams at once — equities, options, news, order events | **All four stayed open.** The connection limit is per feed, not global |
| Second connection to the same feed | **Rejected**, `406 connection limit exceeded`. The incumbent connection survives |

Three protocol details that cost time to discover:

- **The options feed speaks MessagePack, not JSON.** A JSON auth frame is answered with `400 invalid syntax`. Equities and news accept JSON. The client must encode per feed.
- **Order events authenticate with the current format** — `{"action":"auth","key":…,"secret":…}`. The older `authenticate` / `key_id` form still works and returns a deprecation warning.
- Both caps are exact, so the allocation plan above sits at the boundary deliberately: 200 option subscriptions is the ceiling, and the eviction rule exists because of it.

### One owner per feed

A second connection to a feed already held is refused, and the existing connection keeps running. Two consequences:

- **A single process owns all four streams** and fans data out internally. Shadow baselines, the watcher, and the preflight collector read from that one in-memory source rather than opening connections of their own.
- **Ad-hoc scripts run during the scored window will be refused** if they touch a feed the agent holds. The refusal lands on the new connection, so the live agent is never displaced, but any debugging tool needs to read from the running process rather than connect independently.

---
## 6. Runtime state and failure policy

The agent runs unattended across four sessions. Everything below exists because that is true.

### Thesis store

A small append-only store, one record per thesis:

```text
thesis_id · opened_at · hypothesis · evidence_refs · structure · order_ids
           · exit_profit · exit_invalidation · exit_time · exit_news · status · notes
```

Every position traces to a thesis. Every thesis carries its invalidation condition in machine-checkable form, so the deterministic watcher can evaluate it without an LLM. `exit_news` holds a natural-language condition that the triage model evaluates incoming headlines against, which is what makes the news watch set self-maintaining. Cycles read the store first, which stops the agent re-deriving theses it already holds and doubling into exposure it already has.

### Idempotency and restart

- Every order carries a deterministic `client_order_id` derived from `thesis_id`, structure hash, and cycle timestamp. A retry after an ambiguous failure collides rather than duplicating.
- On startup the host reconciles from broker state as the source of truth, then repairs the local store to match. Broker state wins on every disagreement.
- The trade updates stream is subscribed before any order is submitted, so fills are observed rather than polled for.

### Degraded modes

Robustness is named in the judging criteria, so degradation is designed rather than discovered.

| Failure | Response |
|---|---|
| LLM unreachable or over budget | Role falls to the next provider in its chain, substitution logged. Only when the chain is exhausted does the watcher continue alone, exits still firing, no new entries. |
| Generated code fails past the retry limit | Cycle abandoned, logged as a failure with the traceback, next trigger proceeds normally. |
| MCP error mid-submission | Reconcile against broker state, resolve by `client_order_id`, resume or abandon. |
| Quote feed stale or one-sided | Liquidity gate blocks entry. Exits may still use market orders. |
| Risk budget exhausted | Entries blocked, management and exits continue. |
| Any invariant assertion fails | Halt trading, alert, hold current positions. |

Every blocked and rejected order is retained as evidence rather than discarded.

---

## 7. The probability measure

Every ranking depends on a real-world distribution, since risk-neutral pricing assigns approximately zero expected return to every candidate. A single distribution can manufacture edge, and at zero-to-five days to expiry the tail shape dominates — which matters most in exactly the comparison we care about, long gamma against defined-risk credit, since those hold opposite tail exposures.

Three independent measures, therefore, and a candidate is judged on whether it survives all of them.

| Measure | Construction | What it is good at |
|---|---|---|
| **A · EWMA lognormal** | 20-session EWMA realized vol, scaled to DTE | Fast, stable, the conventional baseline |
| **B · Empirical block bootstrap** | Resampled blocks of historical returns at matching horizon | Makes no shape assumption; captures observed clustering |
| **C · Fat tail** | Student-t or jump mixture, calibrated to the same sample | Prices the tail that A structurally understates |

`options.evaluate` returns an edge under each, plus the agreement between them:

```json
{
  "edge_model_A": 0.06,
  "edge_model_B": 0.04,
  "edge_model_C": -0.01,
  "ranking_stability": 0.67,
  "rank_by_model": [2, 3, 9]
}
```

**A candidate attractive under only one convenient distribution is not traded.** `ranking_stability` is the fraction of measures under which the candidate holds its rank band, and it is a gate rather than a decoration — a structure whose appeal evaporates when the tail is modelled honestly was never edge, it was a modelling artifact.

The disagreement is informative in itself. Long gamma tends to look better under C than under A; credit spreads tend to look better under A than under C. A candidate that leads under all three is a genuinely different object from one that leads under its favourite.

**Stress.** Headline rankings are recomputed at ±20% on the volatility input and under a widened spread assumption, on top of the three-measure comparison.

**Calibration.** One script over historical daily bars fits all three to SPY and QQQ and reports where current implied sits against each.

### Normalized candidate representation

```json
{
  "structure": "SPY 09/03 770/775 call vertical",
  "capital_required": 4200,
  "max_loss": 4200,
  "max_gain": 800,
  "edge_vs_price": 0.06,
  "p_profit": 0.44,
  "p_large_gain": 0.09,
  "spread_pct": 0.018,
  "delta": 0.11, "gamma": 0.28, "vega": -0.41, "theta": 0.19,
  "dte": 3
}
```

---

## 8. Execution and fills

Paper fills price against live quotes, so crossing the spread is the real cost and no additional slippage is simulated. Getting filled at all determines whether P&L exists.

- **Universe.** SPY, QQQ, IWM, plus a small set of high-volume single names, where multi-leg fills are realistic.
- **Tenor.** Zero to five days to expiry, so the scored window is most of the contract's life and gamma dominates.
- **Warm-up window.** No entries in the first ten minutes of the session. Option spreads are at their widest into the opening auction, and a gate calibrated on mid-session quotes misbehaves at 09:30 in both directions.
- **Order construction.** `order_class: "mleg"` with a `legs` array carrying `symbol`, `ratio_qty`, `side`, and `position_intent`, submitted as a limit order through MCP with a deterministic `client_order_id`.
- **Fill management.** Open at mid, reprice toward the far side in fixed steps on a timer, cancel and reassess on timeout, reconcile partial fills before the next decision. A partially filled multi-leg structure is repaired or closed before any new entry.
- **Liquidity gate.** Two-sided quotes on every leg and a maximum spread as a fraction of mid, enforced in the host before submission. The threshold is calibrated against the **Indicative feed** we actually receive rather than against OPRA-derived intuitions.
- **Exits.** Stop and stop-limit orders are available for options through MCP and back the time-stop and invalidation exits.

### Early assignment

SPY, QQQ, and IWM options are **American style**, confirmed from the contracts endpoint. A short leg can be assigned before expiry, which delivers stock, changes the risk profile, and consumes buying power mid-week.

- `trading.reconcile` detects an equity position appearing where an option leg was and raises an assignment event.
- Assignment triggers an immediate decision cycle rather than waiting for the schedule.
- Short legs that go deep in the money with a dividend or expiry approaching are flagged for pre-emptive close by the deterministic watcher.

### Window close

The book reaches target posture by **Thursday September 3, 16:00 ET**. Positions expiring that day settle into the measured equity through exercise and assignment.

Overnight gap exposure into Friday's open is minimised. This is not precautionary: the BLS Employment Situation for August 2026 is scheduled for **Friday 4 September at 08:30 ET**, one hour before a 09:30 snapshot. Combined with the ambiguity in Alpaca's own measurement text, carrying risk into Friday's open means carrying the largest scheduled macro event of the window, entirely outside the intended strategy, on a reading of the rules that may not even be the one used.

---

## 9. Agent loop

Monitoring is continuous and free. Deciding is rare and expensive. The loop exists to keep those two facts apart.

Because all four streams are held in one process, everything a decision needs is already in memory. Preflight is a snapshot rather than a fetch, so triggering carries no data cost and the only cost of a cycle is the model call itself.

### Session state machine

```text
        09:30                09:45                        15:45          16:00
  ────────┬────────────────────┬────────────────────────────┬──────────────┬────────
          │      WARM-UP       │          ACTIVE            │ WINDING DOWN │  CLOSED
          │  streams live      │  full loop                 │ exits only   │
          │  window centred    │  entries permitted         │ no entries   │
          │  no entries        │                            │              │
```

- **WARM-UP** — spreads are widest into the opening auction, so the watcher runs, the option window centres on the opening price, and the rolling series starts filling. No entries.
- **ACTIVE** — the full loop. Entries, adjustments, and exits.
- **WINDING DOWN** — exits and repairs only, so the book is deliberate rather than accidental at the close.
- **CLOSED** — reconcile, mark the book, write the daily trace segment. Tier 0 idles.

Thursday September 3 is the last scored session. Its WINDING DOWN begins at 15:00 rather than 15:45, and its objective is the final posture rather than an overnight one.

### Three tiers

**Tier 0 — continuous, no LLM.** Consumes the four streams, maintains the rolling series, marks the book with locally computed Greeks, evaluates exit conditions from the thesis store and acts on them, re-centres the option window, detects assignment, and enforces the rate limiter. Most of what the system does in a session happens here at no model cost.

**Tier 1 — trigger evaluation, no LLM.** Recomputes the derived signals below on each tick and tests them against thresholds. Nothing escalates until a predicate fires.

**Tier 2 — decision cycle, LLM.** Preflight snapshot, then a generated program that spans hypothesis to execution.

### Continuously derived signals

The rolling series makes these local computations rather than questions for the model. Tier 0 keeps them current, and they serve as both trigger inputs and observation-bundle fields.

```text
realized_vol_intraday    from the in-memory minute series
iv_atm                   local Black-Scholes over streamed option quotes
iv_rv_ratio              the core volatility-state signal
expected_move_consumed   session range against the chain-implied daily move
portfolio_delta / vega   summed from marked positions
pnl_vs_max_loss          per position, and for the book
time_remaining           to session close, and to Thursday 16:00
oi_weighted_gamma        open-interest-weighted gamma by strike, with sign
                         change and concentration; refreshed with the daily
                         contract cache since open interest updates overnight
```

The volatility-state comparison is our central edge signal, and it costs nothing to maintain. It belongs in Tier 0 as a number rather than in Tier 2 as a discovery.

### Preflight observation bundle

What the system needs to look at is the same on every cycle. Collecting it deterministically means the model's first token is spent forming a hypothesis rather than writing data-fetching boilerplate.

Tier 0 already holds streamed prices, the rolling series, and account state, so the bundle is a snapshot of warm local state rather than a round of fetching. It is a digest rather than a dump:

```text
trigger        which predicate fired, its measured value, time since last cycle
account        equity, cash, buying power, options buying power, margin used
book           open positions with marks, local Greeks, unrealized P&L against max loss
theses         open theses with hypotheses, exit conditions, and distance to each
universe       per underlying: spot, session range, EWMA realized vol, ATM IV,
               IV/RV ratio and percentile, chain-implied expected move, term structure
liquidity      per candidate expiry: top-of-book spread distribution, quoted depth
calendar       session boundaries, time remaining in the scored window, DTE per position
diff           what changed since the previous cycle
```

The `diff` block matters more than its size suggests. Absolute state invites the model to re-derive what it already concluded; a statement of what moved since 11:00 focuses the turn on novelty and shortens the context.

Depth stays available on demand. A full chain across every strike is far too large for the bundle, so `universe` carries summary statistics and the program calls `options.chain()` when a hypothesis needs individual strikes. Broad and shallow deterministically, narrow and deep on request.

Four properties follow from collecting this way:

- **Fail cheap.** A broken feed surfaces before any tokens are spent.
- **Reproducible.** The bundle is hashed and stored, so a cycle replays exactly. A program-authored observation would differ run to run and make cycles incomparable.
- **Cacheable.** A stable bundle schema keeps the prompt preamble byte-identical across cycles, so provider-side prompt caching applies to everything except the payload.
- **Symmetric with replay.** The replay bundle has the same shape, built by the same clock-aware collector, so prompts transfer unchanged and look-ahead is prevented by construction rather than by discipline.

The division of labour is clean: preflight answers what is true right now, and the generated program answers what would need measuring to test this particular idea.

Bundle fields are extensible by the agent. A program that repeatedly derives the same quantity can register it through `learned.save`, and subsequent cycles receive it in `obs` without writing code for it.

### What escalates to Tier 2

| Trigger | Condition | Expected frequency |
|---|---|---|
| **Session anchors** | 09:45 · 11:00 · 14:00 · 15:30 ET | 4 per session |
| **Underlying move** | Session range consumes more than 0.5× the chain-implied expected daily move since the last cycle | 1–2 per session |
| **Volatility shift** | `iv_rv_ratio` moves more than 10% relative since the last cycle | under 1 per session |
| **Position P&L band** | Unrealized crosses the profit target, or the credit-multiple stop on short-premium structures | event-driven |
| **Greek drift** | Portfolio delta or vega leaves its budgeted band | event-driven |
| **Fill completion** | A structure fills, opening a new management question | 1 per entry |
| **Assignment** | Equity position appears where an option leg was | rare, urgent |
| **Tenor roll** | An open position enters its final session | 1 per position |
| **News salience** | Triage flags a headline as material for a held or watched underlying | 1–3 per session |
| **Deployment floor** | No position open by Monday 10:30 ET | once, at most |

Anchors are chosen rather than uniform. 09:45 follows the opening auction once spreads normalise; 15:30 is the last point at which an overnight decision can be made deliberately.

### News as a trigger

Alpaca provides a Benzinga-sourced news stream, and every article carries a `symbols` array, so the first stage of filtering costs nothing.

Measured over the regular session of 2026-08-28: **269 articles, 41 per hour**. Of those, 7.4% touched SPY, QQQ or IWM, 7.8% touched a mega-cap, and **84.8% touched neither**. The residual is small- and mid-cap earnings reactions and analyst-forecast items — *"These Analysts Revise Their Forecasts On Autodesk"*, *"Quoin Pharmaceuticals Stock Surges Friday"*.

That distribution decides the design. Our tradeable universe is three ETFs plus a shortlist, chosen because multi-leg option fills are realistic there. A headline about a mid-cap is unactionable regardless of how interesting it is, so broad ticker discovery has no path to a trade.

**Discovery is index-relevant rather than symbol-relevant.** The system never finds a new ticker to trade. It finds information that changes its view of the index it already trades.

Three lanes:

| Lane | Source | Volume | Action |
|---|---|---|---|
| **Position-keyed** | `symbols` intersects a held underlying | rare | Always escalate. A thesis invalidating unnoticed is the expensive miss. |
| **Universe-keyed** | `symbols` intersects SPY/QQQ/IWM or the shortlist | ~7% | Queue for the next cycle, escalate on debounce. |
| **Index-relevant** | Mega-cap constituents by index weight, and macro items | ~8% plus an unsymboled tail | Triage model scores salience; escalate above threshold. |

Lanes one and two are deterministic set intersection and cost nothing. The triage model earns its place in lane three, on two jobs symbol matching cannot do: judging whether a mega-cap item is large enough to move the index, and catching macro, rate, and policy headlines that arrive with no useful symbols attached. That residual is a few dozen items per session.

This is the **Featherless** role — high frequency, low cost, narrow classification — with Nebius reserved for the reasoning tier.

**The watch set derives from open theses.** Every thesis carries a news-shaped invalidation condition written by the agent when it opens the position, and the triage model evaluates headlines against those conditions. The agent therefore defines what news matters to it, and a static keyword file never has to be maintained.

**What news is worth.** Index-moving headlines are priced within seconds, and a ten-minute debounce on 0–5 DTE structures will not beat the tape on a print. The value is defensive: knowing why a position is moving against its thesis, and reading a regime change early enough to exit. News remains a trigger into the normal decision cycle, and event volatility stays excluded as a strategy family.

### Prompt composition

Three layers, two of them hot-swappable without touching the third, plus the per-cycle payload. The split matters twice: the large layers are cacheable, and the domain layer can be tuned mid-competition without disturbing runtime semantics.

| Layer | Scope | Changes |
|---|---|---|
| **Core** | Runtime, tool semantics, data-validity rules, execution strategy, output contract. Domain-agnostic. | Frozen after Sunday |
| **Domain skill** | Options mechanics, Alpaca specifics, the probability measure, opportunity families, the risk parameters. Hot-loaded from markdown. | Tunable during the window |
| **Pre-trade skill** | The staged-order review checklist. | Injected **only** on a staged `trading.execute`, never otherwise |
| *Payload* | The preflight bundle. | Every cycle |

The pre-trade layer costs nothing on cycles that do not trade, which is most of them.

**Core layer — byte-identical across every cycle, therefore cached.**

```text
1  Role and objective
     autonomous options agent; scored on total account equity at
     Thursday 16:00 ET; bounded risk; four sessions
2  Hard constraints
     paper account only · options must be involved · defined risk only at
     level 3 · in-window candidates by default · warm-up and winding-down
     rules · quote validity before any decision uses a price
3  Capability API
     every signature with argument and return types, grouped by layer,
     including replay.window, learned.save, and the thesis functions
4  The Structure type and the intent output schema
5  The probability measure, stated once
6  Worked examples
     two or three complete programs
7  Failure conventions
     what to do when a gate rejects, when liquidity fails, when evidence
     is inconclusive
```

**Volatile payload — the preflight bundle alone**, a few kilobytes: trigger, account, book, theses, derived signals, liquidity, calendar, diff.

Nothing else varies. The bundle is the only thing between one cycle and the next, which is what keeps the preamble cacheable and makes cycles comparable to each other.

### Worked examples earn their place

The examples are the strongest available lever on program quality, and after the first call they cost nothing. They are chosen to demonstrate the behaviours instructions describe poorly:

- A program that calls `replay.window` to test its own hypothesis against history, and abandons the idea when the measurement fails to support it.
- A program that opens a thesis with explicit price, time, and news invalidation conditions before calling `trading.execute`.
- A program that returns `none` with a stated reason, showing that declining to trade is a first-class result.

A model imitates a demonstrated pattern more reliably than it follows a described one. Behaviour that matters — falsify before trading, record the thesis, decline cleanly — is shown rather than instructed.

### The pre-trade skill

Injected when an order is staged, and structured as checks with verdicts rather than advice. Each item exists because of a specific observed failure, and each is answered PASS, FAIL, or N/A in the next `thought` before confirming or revising.

```text
ECONOMICS
  1  max profit > 0. A net debit at or above the spread's own width is a
     guaranteed loss at every outcome. Seen in the field as a spread priced
     to max profit −$341.
  2  risk/reward above the floor, computed from the same legs that will ship.
  3  net price recomputed at buy-the-ask and sell-the-bid, not at mid.
  3a every leg has a strictly positive bid. A zero-bid leg cannot be
     exited at any price, which turns bounded max loss into certain loss.

STRUCTURE
  4  every leg is a currently-listed contract, verified this cycle.
  5  position intents correct on every leg; a wrong intent silently changes
     the structure into a different trade.
  6  all legs share the underlying, and the expiry is inside the scored window.

EXITS
  7  long premium carries no drawdown stop. A stop there liquidates the
     convexity the premium was bought to own.
  8  short premium carries a credit-multiple stop.
  9  every position has a time stop tied to Thursday 16:00 ET.

STATE
 10  thesis recorded, with price, time, and news invalidation conditions.
 11  no duplicate or opposing exposure already open on this underlying.
 12  risk budget checked against realised losses, not only unrealised.
```

### Prompt versioning

The preamble is a versioned artifact with its own hash, recorded in the trace alongside the provider, the model, and that provider's determinism controls. A change to the preamble changes the hash, so any shift in behaviour across the window is attributable to a specific revision rather than to drift.

### Cycle contract

- **Serialized.** One cycle runs at a time. Triggers that fire during a cycle are coalesced and evaluated once it completes, so a volatile tape produces one considered decision rather than overlapping ones. Tier 0 continues throughout, so exits still fire while the model is thinking.
- **Bounded.** A cycle has a wall-clock budget of 90 seconds and at most three analysis rounds. Exceeding either abandons the cycle, which is logged as a failure and leaves the book untouched.
- **Closed outcome set.** Every cycle terminates in exactly one of `EXECUTED`, `NO_TRADE`, `BLOCKED_RISK`, `BLOCKED_LIQUIDITY`, `DEGRADED`, `ERROR`. The host validates the outcome against what actually happened and refuses `EXECUTED` when no order was acknowledged. Declining to trade is a first-class recorded result rather than something indistinguishable from a crash.
- **Explicit intents.** An executing cycle returns intents of `open`, `close`, or `adjust`, each carrying a thesis and its exit conditions.
- **In-window by default.** Candidate enumeration runs over the 132 streamed contracts, which are already the near-the-money short-dated structures the strategy wants. Reaching outside costs either a window re-centre or a REST burst, and the program states which it is doing.

### Debounce and budget

- Ten minutes minimum between cycles. Assignment, fill completion, and the deployment floor are exempt.
- Twenty cycles per session, hard cap.
- Entries are blocked in WARM-UP, in WINDING DOWN, and when the risk budget is exhausted. Exits are never blocked.

### Exit policy

Every position carries written exit conditions at entry. The policy differs by premium direction, and the difference is measured rather than assumed.

- **Long premium (net debit).** Maximum loss is the premium paid and is bounded at entry. A mark-to-market drawdown stop liquidates exactly the convexity the premium was bought to own, protecting a left tail that is already capped. These carry a profit target, a thesis invalidation, and a time stop, and **no drawdown stop**.
- **Short premium (net credit).** Loss is bounded by the spread width rather than by the credit received, so it can substantially exceed the premium taken in. These carry a stop at a multiple of the credit, alongside the profit target and time stop.

Risk on the long side is controlled by deployment rather than by stops. Total premium at risk is capped as a percentage of equity, and once cumulative **realised** losses pass a threshold the agent stops opening new positions without liquidating open ones.

**Deployment floor — a forced decision, not a forced trade.** With no position open by Monday 10:30 ET, a cycle is triggered and must terminate in a recorded outcome. `NO_TRADE` remains available, and it must name the specific gate or economics test that failed; "waiting for a better setup" is not an accepted reason.

This addresses the failure mode where a well-instrumented agent holds cash through the entire window, without the worse failure of executing negative-expectancy structures because a clock struck. Repeated `NO_TRADE` outcomes are visible in the trace and are themselves reviewable.

### Adaptive thresholds

After a cycle the model may propose revised trigger thresholds. The host validates them against fixed bounds before applying, so a threshold can be tuned but never set low enough to cause a storm. Revisions are recorded in the trace, which makes the loop's own adaptation visible alongside its trading.

### Cost

Roughly 12–15 cycles per session, near 55 across the window. Preflight removes the observation round, leaving about two model calls per cycle including repair and refinement, so the competition costs a few hundred calls. A stable preamble keeps most of each prompt cacheable. Uniform five-minute polling would produce 78 cycles per session for no additional decision quality.

---

## 10. Opportunity families

```text
Directional        vertical call and put spreads
Convexity          long straddles, strangles, gamma positions
Volatility premium defined-risk credit spreads, iron condors
Relative value     substitute exposure across correlated underlyings
```

Event volatility is excluded: Alpaca serves no earnings calendar and the scored window falls between reporting seasons. Undefined-risk premium selling is unavailable at level 3, so all premium selling is defined-risk.

---

## 11. Verification and safety

```text
generated Python → capability stub → policy verifier → execution adapter → Alpaca MCP
```

### Risk parameters

Starting values. The agent may propose revisions within fixed bounds; the host validates before applying, and every revision is recorded in the trace.

```text
MAX_TOTAL_PREMIUM_AT_RISK_PCT    40.0   all open positions, % of equity
MAX_SINGLE_POSITION_PCT          15.0   one structure, % of equity
REALISED_LOSS_THROTTLE_PCT       12.0   cumulative realised loss that stops new
                                        entries; never liquidates open positions
MAX_CONCURRENT_POSITIONS          8
MAX_POSITIONS_PER_UNDERLYING      4
MAX_SPREAD_PCT_OF_MID             8.9   entry liquidity gate, per leg
MAX_SPREAD_ABS                    0.22  allowance for tick-sized spreads
SPREAD_PCT_CEILING               25.0   the allowance never rescues this far
MIN_BID                           0.01  a zero bid means no exit exists
MAX_QUOTE_AGE_S                  90.0   staleness budget
MIN_RISK_REWARD                   0.25  max profit over max loss
PROFIT_TARGET_PCT                50.0   of max profit, both premium directions
SHORT_PREMIUM_STOP_MULTIPLE       2.0   × credit received, short premium only
```

The spread gate passes on **either** test. Percentage of mid is unstable on cheap
contracts — a five-cent spread is 50% of a ten-cent option and 1% of a five-dollar
one — so an absolute allowance carries the tick-sized case. The ceiling stops that
allowance rescuing a near-worthless contract: `$0.02/$0.07` is only a five-cent
spread and still costs 71% of the ask to cross.

Deployment is the control on the long-premium side, since maximum loss there is bounded at entry. The throttle acts on **realised** losses so that an open position is never liquidated to satisfy it.

Hard invariants, enforced in the host:

- paper environment only, asserted at startup and before every order
- competition account ID asserted before every order during the scored window
- valid, currently-listed option contracts
- two-sided market on every leg, with a strictly positive bid
- quote fresh, uncrossed, and at or above intrinsic
- maximum spread as a fraction of mid, threshold calibrated from live session data
- bounded maximum loss by the universal spread rule
- buying-power check against Alpaca's own margin method
- portfolio risk budget and per-underlying concentration limits
- correct multi-leg position intents
- **max profit strictly positive** — a net debit at or above the spread's own width is a guaranteed loss at every outcome
- **max loss strictly positive** — a non-positive worst case implies risk-free arbitrage and is refused fail-closed
- **risk/reward floor** — max profit over max loss above a stated minimum
- account not `trading_blocked` or `account_blocked`
- starting equity within one cent of $100,000 before the first entry of the window
- deterministic `client_order_id` on every submission
- post-order reconciliation

### Gates are procedures, not reminders

A gate is a decision procedure executed before a side effect. The model records each evaluated gate in the thesis record as `YES`, `NO`, or `BLOCKED`, naming the concrete evidence that produced the verdict. The host validates independently and **refuses an `EXECUTED` outcome when any recorded gate reads `NO`**.

Two mechanisms, deliberately: the model's own recorded evaluation makes its reasoning inspectable, and the host's independent check makes the outcome enforceable. Agreement between them is itself evidence; disagreement halts the cycle.

`BLOCKED` is reserved for a legitimate action whose required identifier is unresolved — an ambiguous contract, a stale quote, conflicting account state. A structure that fails on economics or risk is `NO`, not `BLOCKED`.

### Data validity

Alpaca is trusted infrastructure. The failure mode worth defending against is **absent, stale, or malformed data**, not adversarial content, so no prompt budget is spent on injection defence.

What does go wrong is quotes that look valid and are not. Measured on SPY 2026-09-04 calls at Friday's close:

```
strike    bid     ask   spread%   condition
   768   5.07    5.33      5.0%   ok
   790   0.06    0.07     15.4%   ok
   791   0.02    0.07    111.1%   spread beyond any usable range
   860   0.00    0.01    200.0%   ZERO BID — buyable, not sellable
   500 272.05  274.92      1.0%   deep ITM, tightest in the sample
```

**Zero bid is the trap.** A far out-of-the-money contract at bid `0.00` / ask `0.01` can be bought and cannot be sold. Maximum loss stops being a bound and becomes the outcome, because there is no exit at any price. It reads as a cheap lottery ticket and is a guaranteed write-off.

Validity checks, applied to every quote before it reaches a decision:

```text
present        both sides quoted, neither side null
bid > 0        a zero bid means no exit exists; hard reject
not crossed    bid < ask
fresh          quote timestamp within the staleness budget for its class
sane           option price at or above intrinsic value; a quote below
               intrinsic is unusable rather than an opportunity
consistent     streamed price agrees with the REST quote within tolerance
```

A failed check blocks entries and records `BLOCKED_LIQUIDITY`. Exits are never blocked by it — an exit on degraded data is still better than an unmanaged position.

**The spread threshold was calibrated, not chosen.** The original `3.0` would have rejected the at-the-money Sep-4 calls above, which quoted 5.0% at the close — every candidate the strategy exists to trade.

`scripts/calibrate.py` samples the chain by moneyness band and takes the threshold from the at-the-money band on SPY and QQQ, which is what the strategy actually trades. Wider bands are excluded deliberately: percentage of mid inflates on cheap contracts, so a percentile taken across them measures the wrong thing. Saturday's closing sample gave an at-the-money p90 of 5.95%, hence 8.9 at 1.5×, with a $0.22 absolute allowance.

Closing quotes are systematically wider than intraday, so that figure is an upper bound. The script re-runs at 09:35 ET before the first entry.

The same sample flagged IWM independently: 18% of its quoted contracts carry a zero bid, against 7% for SPY and 2.5% for QQQ, which corroborates the open-interest finding and keeps IWM conditional.

Gates are **pure functions over plain data** — no network, no SDK import, no submission path anywhere in the module. Each returns a structured result carrying its name, verdict, and a human-readable reason. That structure is what the PASS/FAIL checklist renders in the trace, and it makes the whole gate set unit-testable without a broker.

The model chooses strategy and allocation. Brokerage reality is fixed by the host.

Pattern day trader rules do not bind at $100,000 equity, so intraday round trips are unrestricted.

---

## 12. Evidence

All competition trading happens in one account, so comparison runs without placing competing orders.

### Shadow baselines

Implemented in `brain/shadow.py`. Fixed policies run inside the agent process, which is required rather than convenient since only one connection per feed is permitted. They run on the same schedule against the same streamed quotes, recording the orders they would have placed and marking a virtual book against live quotes. No orders reach the broker.

| Policy | Description |
|---|---|
| Adaptive agent | The submitted system, trading the competition account |
| Shadow A | Fixed bull call spread, mechanical entry |
| Shadow B | Fixed long straddle, mechanical entry |
| Shadow C | Fixed defined-risk credit spread |
| Shadow D | Flat cash reference |

Because paper fills price against live quotes, a shadow book marked at the quote closely approximates what the paper account would have recorded. The comparison covers one window and is reported as an observation.

### Shock simulation

Explicitly encouraged by Alpaca as evidence of guardrails. The agent writes the scenario grid itself through a generated program — underlying ±1/2/5%, volatility ±20/50%, one session of decay — and the output goes in the repo and the write-up. Having the agent author its own stress test demonstrates the architecture better than shipping the function pre-built.

### Replay-backed falsification

`replay.window` is available to the agent during live decisions, so a hypothesis can be measured against history before it is traded. Historical option quotes do not exist, so replay models the spread with a stated haircut; the write-up names the limitation and reports the sensitivity.

### Adversarial review

The specification was reviewed by a second model on 2026-08-29. Adopted from it: the objective rewritten so convexity is a candidate family rather than the default posture; the deployment floor reduced to a forced decision; the `TradeIntent` → `VerifiedTradeIntent` boundary; three probability measures in place of one; `oi_gamma` renamed from dealer gamma; provenance envelopes on replay; and `learned.save` constrained to pure analysis.

Two of its factual corrections were tested and both held. Alpaca **does** serve Greeks and implied volatility on the indicative feed — measured present on 8 of 8 near-the-money contracts and absent on 3 of 3 zero-bid strikes. Our earlier finding of `None` came from a `snapshots` call limited to 50 results, which returns contracts in symbol order and landed entirely on unquotable far strikes: the same pagination trap this document warns about elsewhere. And the BLS Employment Situation is confirmed for 08:30 ET on Friday 4 September, which turns the Friday gap argument from precaution into fact.

### Evidence ledger

One JSONL file recording, per claim: the claim, run command, data fingerprint, provider, model, model version, the provider's determinism controls, sample size, result, and status.

---

## 13. Models and providers

Alpaca places no restriction on model provider or hosting. Four are supported, behind one shim, selected per role by configuration.

### Two adapter families

| Family | Providers | Client |
|---|---|---|
| **OpenAI-compatible** | Nebius AI Studio · OpenAI · Featherless | One adapter, `base_url` and model id swapped |
| **Anthropic** | Claude | The official `anthropic` SDK |

Anthropic is not reached through an OpenAI-compatible shim. Its message shape, tool schema, caching controls, and sampling surface all differ, and pretending otherwise silently loses the features that matter.

### `prompt_json` is the default tool mode

Every reply is a single JSON object, `{thought, code}`, with `thought` a sentence or two of plan and `code` executable Python.

The shape is stated **in the prompt and parsed from free text**. No `response_format`, no `json_schema`, no native tool calling, no provider-side structured-output parameter anywhere in the codebase — the only API parameters carried are `thinking` and `effort`, which control reasoning depth rather than output shape. That keeps the contract identical across four providers instead of four dialects of the same idea, and it is what makes a fifth provider a config line.

This is what makes four providers cheap rather than four integrations. The agent needs exactly one tool — execute a program — so native tool calling buys almost nothing and costs portability. Native mode stays available per provider for comparison, and the same generated program runs either way.

### Roles

Each role names a provider, a model, and a fallback chain.

| Role | Work | Default | Falls back to |
|---|---|---|---|
| **Decision** | Program generation and reasoning. Low frequency, high capability. | Anthropic `claude-opus-5` | Nebius `moonshotai/Kimi-K3` → OpenAI `gpt-5.5` → Nebius `openai/gpt-oss-120b` |
| **Triage** | News salience. High frequency, narrow, cheap. | Nebius `openai/gpt-oss-120b`, moving to Featherless when its key lands | Anthropic `claude-haiku-4-5` |
| **Critic** | Optional falsification pass on a staged intent. | OpenAI `gpt-5.4` | Anthropic `claude-sonnet-5` |

Kimi sits directly behind Opus because it is the only fallback proven against the real `{thought, code}` contract across full cycles; the others were verified on a toy prompt.

**Four ways a provider stops answering**, all of which fall through to the next model:

| Condition | Handling |
|---|---|
| API exception — connection, 5xx | fall through immediately |
| Rate limit, quota, overloaded, auth — `429`, `529`, `insufficient_quota` | fall through, and skip the repair budget on the next provider since retrying a rate limit wastes the cycle |
| Timeout — 70s wall clock on one call | fall through; a hung provider cannot eat the 90s cycle budget |
| Malformed output — no valid `{thought, code}`, or `code` that does not parse as Python | **regenerate on the same model first**: one attempt plus three typed repairs, then fall through |

The last one is structural rather than cosmetic. `code` is checked with `ast.parse` before the completion is accepted, so a model that returns a description of a program instead of a program is treated as not having answered, rather than costing a sandbox round to discover.

**A malformed reply earns a regeneration cycle before the chain gives up on it** — one attempt plus three typed repairs naming the specific fault, on the same model, exactly as in the bitgn agent. Measured: Kimi given a deliberately loose system prompt returned prose at `repairs=0` and a valid program once repairs were allowed. Every provider gets its own full budget; why the previous one failed says nothing about whether this one can be talked into compliance.

When the whole chain is exhausted the cycle terminates `ERROR` and the trace carries one record naming every model tried and why each failed, which renders in the panel's decision stream.

Reasoning is deliberately **not** required: `gpt-oss-120b` emits none, and demanding it would disqualify a working fallback.

Every substitution is recorded in the trace as `provider_fallback` with the reason, and rendered in the panel.

A provider outage or rate limit moves the role down its chain rather than stopping the agent. This upgrades the degraded-mode row for an unreachable model from *"no new entries"* to *"fail over, and only stop entering if the chain is exhausted."*

### Models

Anthropic, from the current model table:

| Model | Id | Context | Input / MTok | Output / MTok |
|---|---|---|---|---|
| Claude Opus 5 | `claude-opus-5` | 1M | $5.00 | $25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | $2.00 | $10.00 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | $1.00 | $5.00 |

### Measured on the real contract

Each model was given the actual system prompt and asked for a `{thought, code}` object generating an analysis program. Latency is end to end, streamed where the SDK requires it.

| Model | Latency | Output tokens | Contract | Note |
|---|---|---|---|---|
| `openai/gpt-oss-120b` (Nebius) | 1.7 s | 592 | OK | fastest usable |
| `claude-haiku-4-5` | 3.7 s | 319 | OK | |
| `gpt-5.4-mini` | 3.5 s | 448 | OK | |
| `gpt-5.4` | 4.5 s | 286 | OK | |
| `claude-sonnet-5` | 4.7 s | 383 | OK | |
| `claude-opus-5` · effort `low` | 10.0 s | 952 | OK | no thinking block emitted |
| `gpt-5.5` | 11.6 s | 1124 | OK | |
| `claude-opus-5` · default effort | 19.4 s | 1836 | OK | one thinking block |
| `zai-org/GLM-5.2` (Nebius) | 35.5 s | **8000, capped** | **empty** | see below |

**`zai-org/GLM-5.2` is disqualified for program generation.** Given an 8000-token budget it consumed the entire allowance on reasoning and returned zero characters of content, stopping only at the cap. It answers short prompts fine — this is a code-generation failure specifically, and it fails by exhausting the budget rather than by returning early. Any reasoning model in the decision role needs this exact test before it is trusted with a cycle.

**Effort is a real lever on Anthropic.** Dropping `claude-opus-5` from default to `effort: "low"` halved both latency and output tokens, produced no thinking block, and still returned a valid contract with comparable code length. Worth using for routine cycles and reserving default effort for cycles that actually stage an order.

The 90-second cycle budget accommodates every model above, so capability rather than latency drives the decision role.

### Differences that bite

**Sampling parameters do not exist on current Anthropic models.** On `anthropic` 1.2.0, `temperature` is not even a parameter of `messages.create` — passing it raises a `TypeError` client-side rather than reaching the API. `top_p` and `top_k` are likewise gone, and `budget_tokens` returns a 400. Depth is controlled by `output_config: {effort: "low" … "max"}`, and thinking is `{type: "adaptive"}` or omitted.

This corrects a claim made earlier in this document. **Reproducibility pinning is per-provider**, not one uniform tuple:

```text
OpenAI-compatible   model id · version · temperature · seed
Anthropic           model id · effort level · thinking mode
```

Both are recorded in the trace under the provider that produced them. A cycle reproduces within its provider; it does not reproduce across providers, and the trace says which one ran.

**Caching is automatic on one side and explicit on the other.** OpenAI-compatible providers cache the prefix on their own. Anthropic needs `cache_control: {type: "ephemeral"}` breakpoints, at most four per request, over a render order of tools → system → messages. Our three prompt layers map onto that directly — one breakpoint after the core layer, one after the domain skill, leaving the payload uncached and two breakpoints spare. Cache effectiveness is checked against `usage.cache_read_input_tokens` rather than assumed.

**Reasoning consumes the budget before visible content**, on both families. `zai-org/GLM-5.2` returns empty `content` at small `max_tokens` because reasoning ran first; Anthropic bills thinking and defaults `display` to `omitted`. `max_tokens` is set generously in both cases, and large values stream rather than blocking.

**No assistant prefill** on current Anthropic models. Output shape is controlled by the prompt contract, which the `{thought, code}` schema already provides.

### Keys

`NEBIUS_API_KEY`, `ANTHROPIC_API_KEY`, and `OPENAI_API_KEY` are configured. `FEATHERLESS_API_KEY` is present but empty and lands later; until it does, triage runs on Nebius.

A role whose key is absent is skipped and its fallback used, with the substitution logged. The loader resolves a small alias set per provider — `OPENAI_API_KEY` also accepts `OPEN_AI_API_KEY`, and the Alpaca secret accepts `SECRET` — so a hand-edited `.env` degrades to a logged warning rather than a silently disabled provider.

Running one architecture across providers produces a comparison of decision quality, latency, and cost, and supplies the Featherless integration required for the partner prize.

## 14. Trace and write-up

No user interface is required, so the deliverable is the repository, the trace, and the write-up.

The trace is JSONL from the first line of code, rendered to a static HTML report at the end. Every generated program is stored verbatim and hashed, linked to the trigger that caused it and the orders it produced.

```text
TRIGGER → PREFLIGHT → HYPOTHESIS → PROGRAM → EVIDENCE → CANDIDATES → VERIFICATION → ORDER → FILL → RECONCILIATION
```

The report shows the firing trigger, the preflight bundle and its hash, the generated program in full, the evidence it produced, competing candidates with normalized economics, the allocation decision, the PASS/FAIL gate, the Alpaca order ID and fill, and equity against the shadow baselines. The `learned/` diff shows which helpers the agent wrote for itself during the run.

Verification renders as a checklist:

```text
SPY 09/03 770/775 CALL VERTICAL

✓ two-sided quotes on both legs
✓ spread 1.8% < 3.0% limit
✓ max loss $4,200 bounded
✓ risk budget available
✓ no duplicate exposure
✓ position intents valid

EXECUTABLE
```

The README discloses pre-event setup, names every third-party package and prebuilt component, and states the account ID.

### Telemetry

The trace is also emitted as OpenTelemetry GenAI spans to an external collector, so
the run is observable while it happens rather than only after it. One decision cycle
is one trace:

```text
invoke_agent alpaca_options_agent
  chat <model>                token usage, reasoning tokens, input/output messages
  execute_tool run_program    carries the chat's gen_ai.tool.call.id
    execute_tool market.spot  every capability call the program made
    execute_tool options.enumerate
    ...
```

This shape is the honest one for a code agent. The model makes one decision per
round; the program it wrote makes many tool calls. Only `run_program` carries an id
the model actually emitted, so only that span joins back to model reasoning. The
capability spans beneath it are recorded without one, because none exists — the
program called them, not the model.

Provider reasoning is normalised into a `reasoning` part and never merged into
public text. Anthropic needs `thinking: {display: "summarized"}` for this to carry
any content at all; the default is `omitted` and the blocks arrive empty.

Content capture is opt-in through `OTEL_CAPTURE_CONTENT`. Structural spans emit
either way. Setup failure disables emission rather than raising, and every span
helper is a no-op when disabled, so telemetry can never break trading.

---

## 15. Build status

Implemented and verified. **169 tests.**

| Component | Module | State |
|---|---|---|
| Profiles, credential aliases, window guards | `config.py` | done |
| `TradeIntent` / `VerifiedTradeIntent`, gates, thesis | `types.py` | done |
| Black-Scholes, IV solver | `quant/bs.py` | done — validated against Alpaca's Greeks |
| Universal spread rule, payoff | `quant/structures.py` | done — matches Alpaca's worked example |
| Realized vol with daily-bar fallback | `quant/vol.py` | done |
| Three probability measures, rank stability | `quant/measures.py` | done |
| Deterministic structure enumeration | `quant/candidates.py` | done |
| REST client, two token buckets | `host/rest.py`, `host/limiter.py` | done |
| Four websocket streams, health checks | `host/streams.py` | done — all four hold concurrently |
| Rolling in-memory series | `host/series.py` | done |
| Pure gate module | `host/gates.py` | done |
| Two-phase executor, fill management | `host/execution.py` | done — **never met a live broker** |
| Thesis store | `host/thesis_store.py` | done |
| JSONL trace | `host/trace.py` | done |
| OpenTelemetry emission | `host/telemetry.py` | done — delivery confirmed |
| Capability dispatch | `host/capabilities.py` | done |
| Sandbox, pipe RPC, repair hints | `sandbox/` | done |
| Four-provider shim, contract parsing | `brain/providers.py` | done |
| Three prompt layers, Anthropic cache blocks | `prompts/`, `brain/prompt.py` | done |
| Preflight bundle with diff | `brain/preflight.py` | done |
| Tiered trigger loop, session state | `brain/loop.py` | done |
| Shadow baselines | `brain/shadow.py` | done — four fixed policies, no orders |
| Runner | `run.py` | done — soak tested |
| Warm-up protocol | `scripts/warmup_check.py` | done — GO/NO-GO, 8 checks |
| Spread and volatility calibration | `scripts/calibrate.py` | done |
| Token and cost estimator | `scripts/estimate_cost.py` | done |
| VPS deployment | `deploy/` | done — systemd, hardened, no listening port |

### Not built, deliberately

- **Historical replay arena.** Alpaca serves no historical option quotes, so replay
  would model the spread — meaning the fill and liquidity gates, the parts most
  worth testing, would be assumptions. Cut in favour of live shadow baselines.
- **`oi_gamma`.** Specified in §4, unimplemented. The signal is real but it was not
  worth building ahead of the execution path.
- **Featherless triage tier.** Wired in the provider chain, key not yet present;
  triage falls back to Nebius.
- **Per-order corporate-action gate.** Checked and found not binding: no corporate
  action falls inside 31 Aug – 3 Sep on our universe. Reduced to a session-start
  check that warns if one appears.

### Found by building, not by reading

Each is now a regression test.

- `session_state` reported `WARM_UP` at 09:34 on a **Saturday**: time of day alone,
  no trading-day check. It would have tried to trade on a weekend.
- `market.bars` returned 403 on its own default `end=today`, which trips the
  15-minute historical restriction.
- `vol.realized` returned a dict after the fallback was added while the prompt still
  documented a float; the mismatch cost a whole cycle round.
- `parse_contract` let a raw `JSONDecodeError` escape the repair loop on a truncated
  reply, crashing the cycle instead of repairing it.
- `obs` was a plain dict while the prompt taught `obs.universe[...]`, burning a round
  on the first live cycle.
- Ranking candidates by risk/reward put every unbounded-profit structure first,
  because `inf` always wins — the model saw sixty straddles and no verticals.
- `MAX_SPREAD_PCT_OF_MID = 3.0` would have rejected every candidate. Now 8.9 from
  the measured at-the-money p90, paired with a $0.22 absolute allowance because
  percentage of mid is unstable on cheap contracts — and a 25% ceiling, because a
  five-cent spread on a four-cent option is still 111% to cross.
- Anthropic emitted no reasoning until `thinking: {display: "summarized"}` was set,
  and cached nothing until explicit `cache_control` blocks were added. Both were
  invisible while testing on OpenAI-compatible providers, which cache automatically.
- OTel context is thread-local, so capability spans raised on the sandbox's serving
  thread detached from the program span until the context was propagated.

### Verified against live APIs

- All four streams hold simultaneously; equity caps at 30 symbols, options at 200,
  both exact.
- Local delta and gamma agree with Alpaca's to under 0.5%; our IV runs ~0.8 points
  below theirs on one-week contracts, so implied is compared against our own
  realized under the same formula.
- Greeks are present for every near-the-money contract and absent exactly where the
  quote is unusable.
- The three measures disagree as designed: a call credit spread scored +0.123 under
  the lognormal and −0.028 under the empirical bootstrap, and was rejected.
- Anthropic prompt caching engaged on the second run: 3,953 of ~4,600 input tokens
  read from cache.
- Telemetry delivery confirmed by OTLP/HTTP `200 {"partialSuccess":{}}`.
- The competition account has **zero orders of any kind, ever**.

### The risk that remains

The system has never met a live tape. Friday 28 August closed before the build
finished and the market is shut all weekend, so the first live market this code sees
is the scored window itself. Specifically untested: an option quote arriving over
the websocket, an order filling, the repricing ladder in `manage_fill`, and the
rolling series producing an intraday realized-vol figure.

`scripts/warmup_check.py` exists to compress that risk into 09:30–09:45 on Monday,
on the development account, before entries open. It returns GO/NO-GO.

---

## 16. Build order

**Saturday — host and capabilities**
Sandbox with pipe RPC and policy verifier. Data plane: three stream subscriptions, rolling series, token-bucket limiter, session contract cache. Layer 1 primitives against MCP. Local Black-Scholes for IV and Greeks. `Structure` type and thesis store. Execution layer with fill management, deterministic `client_order_id`, and trade updates stream. `risk.max_loss` by the universal spread rule.

**Sunday — decision system**
`tradeable_chain`, `enumerate`, `evaluate`. Probability measure and calibration script. Tier 0 watcher, Tier 1 triggers, preflight collector. Decision program prompt and the three-round contract. `replay.window` and `learned/`. Shadow baselines. Trace logging. End-to-end dry run on the development account in `propose` mode, then one live round trip in `execute` mode.

**Monday 09:30 ET — live on the competition account.** Warm-up until 09:45, first decision cycle at the anchor.

**Monday to Thursday** — supervise, tune triggers and gates, Featherless triage, shock simulation, daily social posts.

**Thursday 16:00 ET — scored window closes.** Target posture reached, gap exposure minimised.

**Thursday evening to Friday** — write-up, video, slides, static trace report.

**Friday 11:00 ET** — submit.

---

## 17. Deployment

The agent runs Monday 09:30 through Thursday 16:00 unattended, so it runs on a VPS
rather than a laptop that can sleep or lose a network.

**It listens on no port.** Every connection is outbound: websockets to Alpaca, HTTPS
to the model providers, gRPC to the OTel collector. No firewall rule is added and
none is needed, which is a smaller surface than allocating one.

| | |
|---|---|
| Unit | `alpaca-agent.service` |
| User | dedicated `alpaca` system user, `nologin` |
| Directory | `/opt/alpaca-agent`, mode 750, `.env` mode 640 |
| Confinement | `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`, writes limited to the run directory |
| Limits | 1 GB memory, 256 tasks, restart capped at 5 in 10 minutes so a crash loop cannot hammer Alpaca |
| Co-tenants | `xray` (443), `mtproxy` (1443), and the existing `control.service` (3000) are untouched |

Access is key-based. The deploy key is dedicated to this service rather than reused.

**Exactly one agent may run anywhere.** Alpaca refuses a second stream connection
per feed per account with 406 and the incumbent survives, so a second process
against the same account fails quietly while appearing healthy. That is the failure
mode most likely to cost a session, and `systemctl stop alpaca-agent` is the switch.

`deploy/README.md` carries the commands.

---

## 18. Submission checklist

| Item | Notes |
|---|---|
| Project title, short and long description | |
| Technology and category tags | |
| Cover image | |
| Video presentation | Live trace walkthrough with real order IDs |
| Slide presentation | |
| Public GitHub repository | Private during the event, public at submission. `.env` excluded |
| Demo application platform and URL | No UI required; repository is sufficient |
| **Alpaca paper account ID** | Read from private runtime configuration when completing the form |
| Social posts, up to 5 | X and LinkedIn, tagging `@lablabai` and `@AlpacaHQ` |
| MIT `LICENSE` in the repository | Required — submissions must be MIT-compliant |
| README disclosures | Pre-event setup, third-party packages, prebuilt components, SDK justification |

Social engagement is a scored criterion on the event page and carries two separate $500 prizes. Posting starts Monday.

---

## 19. Success criterion

The project succeeds when:

> An autonomous agent wrote and executed its own decision programs across four scored sessions, measured its own hypotheses against replayed history before trading them, extended its own capability API during the run, and passed every order through deterministic risk gates. Blocked orders, degraded-mode events, and shock scenarios are on record as evidence of the guardrails, and the decision trace reproduces end to end from real Alpaca order IDs and fills.

If the adaptive agent trails its shadow baselines over the window, the write-up reports it. Four sessions is a small sample, and an honest negative result reads better than a defended one.
