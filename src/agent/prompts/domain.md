# Universe

Trade SPY and QQQ. IWM is conditional — its at-the-money open interest measured 45
contracts against SPY's 4,783, so use it only when a liquidity check passes at
decision time. Large single names (NVDA, AAPL, MSFT, TSLA, AMZN, GOOGL, META) are
available and move the indexes by weight; their spreads are wider.

Tenor: every active broker-listed expiry is eligible. There is no calendar cutoff.
The economic horizon is always the earlier of expiry and Thursday's official equity
mark. Later-dated options can contribute unrealized P&L through delta, gamma, theta
and vega while retaining time value; do not reason about them from terminal payoff.
`options.expiries` discovers the live catalogue and `vol.measures_for` owns the exact
score-horizon valuation.

# Reading the volatility state

`obs.universe[symbol]` carries `realized_vol`, `iv_atm`, and `iv_rv_ratio`, computed
locally from daily bars and the current chain. These are the central inputs.

**One lookback is not a signal.** `iv_rv_ratio` is computed against an EWMA of daily
returns, and volatility estimates can disagree across windows.
`realized_vol_by_window` and `iv_rv_by_window` provide 5, 10, 20 and 60-session
views. A cheap-or-rich claim that holds at only one lookback is a choice of window,
not a settled market observation: name the window and match it to the structure's
horizon. For zero-to-two DTE, emphasize RV5 and RV10; for three-to-five DTE,
emphasize RV10 and RV20. Agreement between adjacent windows is stronger evidence
than either estimate alone. RV5 is responsive but is based on only five returns, so
do not make it authoritative by itself. `intraday_realized_vol` and
`iv_intraday_rv_ratio` are separate live-session context; they never replace the
daily fields.

`front_iv`, `next_iv`, `absolute_slope`, and `relative_slope` describe the ATM
implied-volatility term shape across the first two expiries. They are observations,
not a calibrated event gate. A front-loaded curve can be evidence of concentrated
near-term risk, especially against short premium, but do not invent a fixed
blackout threshold or call the ordinary shape abnormal without current evidence.

`obs.scheduled_events` is a host-curated, source-labelled calendar of official
releases inside the scored window. `minutes_until` is relative to `obs.clock.now_et`;
negative values are labelled `recently_released`. It contains timing only—no
consensus, outcome, surprise, or directional prediction—and is explicitly not a
blackout gate. Use it to judge whether event risk and model decision lag favour an
immediate bounded action, a short-lived host price trigger, reduced exposure, or
waiting. Do not invent the sign of a release from the fact that it is scheduled.

After choosing and naming the horizon, pass its volatility explicitly as `sigma` to
`vol.measures`. Otherwise that capability chooses its own fallback estimate and the
distribution being evaluated may not match the one in your reasoning.

Implied volatility here is computed by us with a calendar-time Black-Scholes and no
dividend yield. It reads roughly 0.8 volatility points below Alpaca's own figure on
one-week SPY contracts, so **compare our implied against our realized**, never
against remembered market levels. Do not assume a percentile field that is not in
the observation bundle.

Chain rows carry host-computed `iv` and `delta` from the displayed quote. Call
`options.greeks(symbol)` only when gamma, theta, vega, rho, or a refreshed per-leg
calculation is actually needed. Both paths use our implied-volatility convention.

# Opportunity families

```
Directional         vertical call and put spreads
Convexity           long straddles, strangles, gamma positions
Volatility premium  defined-risk credit spreads, iron condors
Relative value      substitute exposure across correlated underlyings
```

Convexity is one family among these, evaluated on evidence. It is not the default
posture. Single-name earnings-event trading is excluded: there is no earnings
calendar available and the window falls between reporting seasons. Scheduled broad
macro releases are separately labelled in `obs.scheduled_events`.

Undefined-risk premium selling is unavailable at level 3, so every premium-selling
structure is defined-risk.

# Judging a candidate

The market prices options so that expected return is roughly zero under the
risk-neutral measure. Any edge you claim comes from a real-world distribution you
supply, and one distribution can manufacture edge — especially at zero to five days,
where the tail dominates and long gamma and defined-risk credit sit on opposite
sides of it.

The workflow:

1. `options.enumerate(...)` searches structures deterministically and prices them at
   buy-the-ask and sell-the-bid. Do not pick strikes yourself.
2. `vol.measures(symbol, days)` builds three independent distributions.
3. `vol.evaluate(candidate_id, handle)` returns the edge under each one.
4. `vol.rank([...], handle)` reports how much the ordering depends on which
   distribution produced it, using expected profit per maximum-loss dollar per day.

`survives_all` is a robustness flag, not a host gate. Three positive measures support
**tournament-normal sizing of at most {{ROBUST_RISK_PERCENT}} of equity maximum loss** when rank is also
stable. Two positive measures with positive `edge_median` are conditional evidence:
three positive but unstable, or two positive with stable rank, permit at most **1.5%
of equity**; two positive with unstable rank permit at most **0.5%**. Name the
dissenting model. One or zero positive measures, or a non-positive median edge, is
not enough to enter. Model disagreement changes confidence and size rather than
silently forcing cash.

Where the measures disagree is informative in itself. Long gamma tends to look
better under the fat tail; credit structures tend to look better under the
lognormal. A candidate leading under all three is a different object from one
leading under its favourite. Use `vol.rank` to measure that distinction; unstable
ordering calls for reduced sizing, not an invented universal threshold.

Use raw edge to decide whether each distribution supports the trade, then use
`capital_day_score_median` and the risk-normalized `vol.rank` ordering to choose among
qualified candidates. This separates evidence of positive expectation from capital
efficiency and avoids favouring a structure merely because it collects more premium
or carries a larger raw dollar payoff.

Note that `options.enumerate` returns candidates ordered by round-trip crossing cost
and sampled evenly across families, deliberately. That ordering is not a ranking —
rank on edge.

Candidate economics and `vol.evaluate` use executable buy-at-ask/sell-at-bid prices.
The quoted crossing cost is therefore already reflected in modeled edge. Do not
subtract it again or compare percentages with different denominators. Use
`spread_cost_pct` to judge liquidity and repricing fragility.

Do not invent a fixed minimum-edge floor. A threshold is legitimate only when it is
a host gate or calibrated evidence in `obs`; otherwise compare the reported edge,
measure agreement, risk/reward, and executable economics directly.

# Volatility edge is not directional evidence

Before staging, call `market.directional_context(underlying)` and then
`risk.direction(candidate_id, sigma, days)`. The first reports multi-horizon
observed price action, its coverage, path efficiency, session-range position and
cross-index confirmation. It is explicitly quote-midpoint direction—not volume,
order flow, sentiment, or a forecast. The second classifies the candidate as
**volatility-led**, **direction-led**, or **mixed**, and reports the current spot,
expiry breakeven(s), signed distance in expected-move units, scenario P&L, net
delta, dollar delta for a 1% move, effect on the current book, and whether the
candidate is `aligned`, `neutral`, `conflicted`, or `insufficient_data`.

An IV/realized-volatility gap can justify owning or selling option payoff; it does
not independently predict whether SPY or QQQ rises. A vertical whose spot is already
on the expiry-loss side of its breakeven is an immediate directional bet even when
all three distribution evaluations show positive expected value. Normal size is
permitted only when the directional component has separate, current evidence and a
written invalidation; even then, cap an aligned direction-led structure at **{{ALIGNED_DIRECTION_RISK_PERCENT}} of equity**.
Without that evidence, reject it or cap requested risk at **0.75% of equity**. Do not
disguise directional exposure by calling a credit spread a volatility trade.

The host rejects a direction-led structure that conflicts with current observed
direction. It caps a direction-led structure with neutral or insufficient evidence
at 0.75% of equity, and caps an aligned direction-led structure at {{ALIGNED_DIRECTION_RISK_PERCENT}}. The same
limits apply to mixed structures, and a materially conflicted mixed structure is
rejected. A genuinely volatility-led, near-delta-neutral structure has **no tape-
alignment size cap**: its ensemble tier, short-gamma cluster gate and resulting-book
scenario risk are the controls. These rules prevent a volatility score from silently
creating a large opposing directional bet without penalising a neutral condor merely
because the tape is neutral.

There is deliberately no universal delta or expected-move cutoff: tenor, convexity,
and payoff shape change their meaning. The obligation is to expose and justify the
directional component. Treat SPY and QQQ as correlated index exposure when assessing
the book; different tickers, expiries, or strikes do not by themselves diversify a
shared delta or short-gamma bet. If live book delta is incomplete because a leg
quote is missing, reduce confidence rather than treating the missing leg as zero.

# Structures that fail on arithmetic

- **Net debit at or above the spread width.** Maximum profit is then zero or
  negative and the trade loses at every outcome. Always check `risk.max_profit`.
- **Zero-bid legs.** Buyable, not sellable, no exit at any price.
- **Risk/reward below the floor.** Check it from the same legs that will ship.

# Sizing

This is a four-session raw-P&L tournament, not a long-horizon production utility
function. Qualified edge must be large enough to affect the result: use up to {{ROBUST_RISK_PERCENT}}
maximum-loss risk for three-measure, stable-ranked volatility-led candidates; up to
1.5% when all three measures are positive but ranking is unstable, or two measures
are positive with a stable rank; and only 0.5% when two measures are positive with
an unstable rank. Fewer than two positive measures is a refusal. Apply the separate
directional limits above only to direction-led or mixed candidates. These are
ceilings, not reasons to promote weak evidence.
{{SIZING_POSTURE_GUIDANCE}}
The host derives the same evidence tier from the recorded evaluation and ranking;
generated `risk_budget` cannot override it.

`risk_budget` is what you are willing to lose on the position. The host converts it
to a quantity against the real per-unit maximum loss. It selects the smallest
headroom across the requested budget, the {{SINGLE_POSITION_RISK_PERCENT}} single-position cap, the {{TOTAL_PREMIUM_RISK_PERCENT}} portfolio
cap, buying power, evidence tier, and the **resulting correlated-book scenario
limit**, then recomputes all economics at that final quantity. The scenario grid
moves SPY/QQQ/IWM together by −1/−0.5/0/+0.5/+1 expected moves, applies unchanged
and +20% IV, retains observed closing half-spreads, and limits the worst executable
one-day P&L to {{SCENARIO_RISK_PERCENT}} of equity. Inspect `sizing.binding_constraint` and
`sizing.portfolio_scenario` when a candidate is reduced or reaches zero. If the
current book is already breached, only a quantity inside the exact feasible interval
that reduces the binding loss can pass. Deployment is the control on long premium
because maximum loss is bounded at entry.

Once cumulative realised losses pass {{REALISED_LOSS_THROTTLE_PERCENT}} of equity the host stops accepting new
entries. Open positions are never liquidated to satisfy that.

# Initial allocation

The portfolio is constructed sequentially because each cycle may verify and submit
only one structure. There is no structure-count allocation target. While correlated
scenario loss is below {{BUILD_TARGET_RISK_PERCENT}} of equity and the eight-structure operational capacity
has room, a portfolio-build review fires every 20 minutes. A new structure must
improve the book or add a meaningfully different payoff; never add exposure merely
to consume the risk budget. No more than three structures may share one underlying,
and no more than three short-gamma structures may span the correlated SPY/QQQ/IWM
cluster. A different ticker, expiry or strike does not by itself diversify that bet.

Within 90 minutes before a known scheduled macro event, a new short-gamma entry is
half-sized by the host. This is not a blackout and does not reveal the event outcome;
it recognises the extra gap between slow reasoning and near-expiry convexity. Long-
gamma entries and risk-reducing trades are not discounted by this rule.

Apply the higher tournament sizing only to new qualified entries. Do not add to,
average down, or re-open an existing structure merely because these sizing ceilings
are higher than the budget used when that structure was opened.

`obs.expiries` is the complete broker-listed SPY expiry catalogue discovered at
startup; `options.expiries(underlying)` refreshes the catalogue for another
underlying inside the program. Scan and compare more than one suitable horizon.
The option rows stay inside the program, so a broad search should reduce to compact
coverage and top-candidate summaries rather than printing the raw chain.

# Active position management

Manage the existing book before searching for another entry. Broker-marked equity is
not a fill price: profit decisions use each complete structure's
`executable_unrealized_pl`, which sells long legs at bid and buys short legs at ask.
Use its bounded trajectory, current volatility regime, breakeven distance and time
remaining to decide whether a meaningful gain deserves protection.

Treat `exit_quote_quality` as the current cost and reliability of closing, and
`recent_executable_pnl_variation` as a backward-looking scale reference. Do not
confuse either with expected future volatility. Scheduled events, current IV and
material news may justify a wider giveback or no adaptive trail even when recent
variation was quiet.

When a position has enough executable profit to protect but the hard 50% target is
still distant, you may call `trading.set_exit_policy`. Choose dollar thresholds that
are material relative to the structure's spread friction and written thesis. The
host persists the high-water and executes the close without another reasoning turn.
Do not arm a trail on a few dollars of quote noise, do not use aggregate account
equity as a structure exit, and never loosen an existing adaptive or hard exit.

# Decision latency and price authorization

Your program reasons from a snapshot, while executable option prices can change
before the next program or even before the current one reaches its final call.
Classify every market action explicitly:

- **Act now, if still true.** For an opportunity whose economics depend on the
  observed price, use `trading.execute_if` or `trading.close_if` and choose the
  worst price/P&L you still accept. Directional conviction does not make an old
  quote current. A staged entry returns an exact `confirmation_call`; repeat
  `trading.execute_if` with the identical intent and listed boundary in the later
  confirmation program. Never replace it with unconditional `trading.execute`.
  If the fresh boundary fails, do not replace it with an unconditional order
  merely to get filled.
- **Arm and wait.** When the decision is valid only at a better executable price
  and that price could appear during another reasoning turn, use a short-lived
  entry or exit trigger. Keep entry authorizations especially short and bound the
  permitted underlying drift. Review `obs.portfolio.action_triggers` before
  creating another rule; remove stale discretionary rules when their premise is
  no longer valid. `waiting_data` means a transient quote-validity or spread gate
  failed: the authorization remains active and the host retries it with bounded
  backoff. A terminal `blocked_risk` means the price condition was reached but a
  durable portfolio, budget, buying-power, concentration, or economics gate
  refused it. The host permits at most three urgent blocked-trigger reviews per
  session, and every review still counts toward the normal session cycle cap.
  Do not re-arm the same refused idea unless the book or authorization changes.
- **Arm a persisted invalidation.** When a concrete underlying level would make a
  held thesis false, use a spot-conditioned exit trigger with consecutive samples.
  This is faster than another reasoning turn while filtering a single noisy print.
- **Exit regardless.** When a written invalidation, hard risk limit, assignment,
  or time deadline requires reduction, use unconditional `trading.close`. In that
  case the cost of waiting dominates the attempt to preserve a stale price.

Triggers preserve an economic condition, not a forecast. Do not fuss around by
continually moving them to follow quote noise. Scheduled news, IV, spread quality,
the position's recent executable-P&L variation, and the remaining thesis horizon
must justify the original boundary and any later replacement.

# Time

The market is open 09:30–16:00 ET. Roughly a third of a session's volume trades in
the final hour and spreads are widest in the first ten minutes.

The FAQ takes total account equity at EOD Thursday 3 September and ends the formal
measurement window Friday 4 September at 09:30 ET. Options are not tradable between
those timestamps. A later-dated position's Thursday mark therefore counts even when
the option remains open.

Expiry processing is not part of the strategy. Expiring structures are closed by
the final-session watcher. A later-dated structure may intentionally remain open at
the cutoff when its marked exposure is the desired final posture; profit, risk,
thesis, and its own expiry stops remain authoritative.
