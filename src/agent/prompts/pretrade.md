# Pre-trade check

You called `trading.execute(...)`. That **staged** a draft — nothing was submitted.
Work through every check that applies, state the verdict for each (PASS / FAIL / N/A)
in your next `thought`, then confirm or revise.

- **Confirm:** this is now a later model program; call `trading.execute(...)` once
  with the identical persisted intent.
- **Revise:** call it with a corrected intent, which stages a new draft.

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
7. Do all legs share an underlying, and does the expiry sit inside the scored window?

## Exits

8. If this is long premium, is there **no** drawdown stop? A stop there liquidates
   the convexity the premium was bought to own.
9. If this is short premium, is there a credit-multiple stop?
10. Does the position have a time stop tied to Thursday 16:00 ET?

## State

11. Is the thesis recorded, with price, time, and news invalidation conditions?
12. Is there duplicate or opposing exposure already open on this underlying?
13. Was the risk budget checked against realised losses, not only unrealised?

If the checklist shows FAIL on any host gate, do not confirm. Either revise the
structure so the gate passes, or record `NO_TRADE` naming the gate that failed.
