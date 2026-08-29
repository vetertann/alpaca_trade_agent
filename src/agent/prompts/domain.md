# Universe

Trade SPY and QQQ. IWM is conditional — its at-the-money open interest measured 45
contracts against SPY's 4,783, so use it only when a liquidity check passes at
decision time. Large single names (NVDA, AAPL, MSFT, TSLA, AMZN, GOOGL, META) are
available and move the indexes by weight; their spreads are wider.

Tenor: zero to five days to expiry. Over a four-session window a twenty-day option
barely reacts to the move you are betting on.

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

After choosing and naming the horizon, pass its volatility explicitly as `sigma` to
`vol.measures`. Otherwise that capability chooses its own fallback estimate and the
distribution being evaluated may not match the one in your reasoning.

Implied volatility here is computed by us with a calendar-time Black-Scholes and no
dividend yield. It reads roughly 0.8 volatility points below Alpaca's own figure on
one-week SPY contracts, so **compare our implied against our realized**, never
against remembered market levels. Do not assume a percentile field that is not in
the observation bundle.

Chain rows do not contain Greeks. After loading a contract through `options.chain`
or `options.contracts`, call `options.greeks(symbol)` when a decision needs them.
The result is computed locally from the current quote and our implied-volatility
convention.

# Opportunity families

```
Directional         vertical call and put spreads
Convexity           long straddles, strangles, gamma positions
Volatility premium  defined-risk credit spreads, iron condors
Relative value      substitute exposure across correlated underlyings
```

Convexity is one family among these, evaluated on evidence. It is not the default
posture. Event volatility is excluded: there is no earnings calendar available and
the window falls between reporting seasons.

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
   distribution produced it.

**Trade what survives all three.** `survives_all` is a gate, not a decoration. A
candidate positive under the lognormal and the Student-t but negative under the
empirical bootstrap was never edge — it was an artifact of a convenient assumption,
and the bootstrap is the one making no shape assumption at all.

Where the measures disagree is informative in itself. Long gamma tends to look
better under the fat tail; credit structures tend to look better under the
lognormal. A candidate leading under all three is a different object from one
leading under its favourite.

Note that `options.enumerate` returns candidates ordered by round-trip crossing cost
and sampled evenly across families, deliberately. That ordering is not a ranking —
rank on edge.

# Structures that fail on arithmetic

- **Net debit at or above the spread width.** Maximum profit is then zero or
  negative and the trade loses at every outcome. Always check `risk.max_profit`.
- **Zero-bid legs.** Buyable, not sellable, no exit at any price.
- **Risk/reward below the floor.** Check it from the same legs that will ship.

# Sizing

`risk_budget` is what you are willing to lose on the position. The host converts it
to a quantity against the real per-unit maximum loss and caps it at 15% of equity
for one position and 40% across the book. Deployment is the control on long premium
because maximum loss is bounded at entry.

Once cumulative realised losses pass 12% of equity the host stops accepting new
entries. Open positions are never liquidated to satisfy that.

# Time

The market is open 09:30–16:00 ET. Roughly a third of a session's volume trades in
the final hour and spreads are widest in the first ten minutes.

The scored window ends Thursday 3 September at 16:00 ET. Friday does not trade
before the snapshot, and the US employment report lands at 08:30 ET that morning, so
carrying gap risk into Friday is uncompensated exposure outside your strategy.

Expiry processing is not part of the strategy. Anything that matters to the score is
explicitly closed, or intentionally left in a known marked state, before the cutoff.
