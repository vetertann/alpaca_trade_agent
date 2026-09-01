# Pre-trade check

You called `trading.execute(...)`. That **staged** a draft — nothing was submitted.
Work through every check that applies, state the verdict for each (PASS / FAIL / N/A)
in your next `thought`, then confirm or revise.

- **Confirm:** this is now a later model program; call `trading.execute(...)` once
  with the identical persisted intent.
- **Revise:** call it with a corrected intent, which stages a new draft.
- **Decline:** call `decision.no_trade(reason)`. The host discards the unsubmitted
  draft and records the reason; printing `NO_TRADE` alone is only legacy syntax.

Do not call `trading.execute` twice in this program. `awaiting_confirmation` means
the host refused a same-program confirmation; `restaged` means fresh quotes replaced
an expired draft and another model program must review it.

## Economics

1. Is `max_profit` strictly positive? A net debit at or above the spread's own width
   is a guaranteed loss at every outcome. This has shipped in the field as a spread
   priced to a maximum profit of −$341.
2. Is risk/reward above the floor, computed from the legs that will actually ship?
3. Was the net price computed at buy-the-ask and sell-the-bid rather than at mid?
4. Does every leg have a strictly positive bid? A zero-bid leg cannot be exited at
   any price, which turns bounded maximum loss into certain loss.

## Structure

5. Is every leg a currently-listed contract you verified this cycle?
6. Is `position_intent` correct on every leg? A wrong intent silently converts the
   structure into a different trade with different risk.
7. Do all legs share an underlying, is the expiry in `obs.expiries`, and—when it is
   after the cutoff—was it evaluated with `vol.measures_for` at the score horizon?

## Exits

8. If this is long premium, is there **no** drawdown stop? A stop there liquidates
   the convexity the premium was bought to own. For finite-profit debit structures,
   the host target is a fraction of maximum profit; for unbounded-profit structures
   it is a fraction of premium paid or concrete dollar P&L. State the intended
   target in dollars so the fill-resolved host policy is auditable.
9. If this is short premium, is there a 2x-close-debit/50%-max-loss stop?
10. Is `exit_time` an exact `YYYY-MM-DD HH:MM ET` deadline no later than 15:45 ET
    on the earliest option expiry?

## State

11. Is the thesis recorded, with price, time, and news invalidation conditions?
12. Is there duplicate or opposing exposure already open on this underlying?
13. Was the risk budget checked against realised losses, not only unrealised?
14. Does `risk.direction` show `aligned`, `neutral`, `conflicted`, or
    `insufficient_data`, and does the requested risk satisfy the corresponding host
    cap? A volatility score is not a reason to confirm a directional conflict.
15. Does `sizing.evidence_risk_ceiling` match the recorded ensemble evidence, and
    does `sizing.portfolio_scenario` keep the resulting correlated SPY/QQQ/IWM book
    inside the {{SCENARIO_RISK_PERCENT}} executable scenario-loss limit? If the current book is breached,
    confirm only an exact quantity that the host identifies as risk-reducing.

If the checklist shows FAIL on any host gate, do not confirm. Either revise the
structure so the gate passes, or call `decision.no_trade` naming the failed gate.
