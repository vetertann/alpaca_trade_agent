#!/usr/bin/env python
"""Resolve one question: is multi-leg `filled_qty` denominated in spreads or in
leg-contracts?

`realised_pnl` multiplies matched fill quantity by 100 assuming the broker reports
spreads. If it reports leg-contracts instead, realised P&L doubles on a two-leg
structure and quadruples on a condor, and the realised-loss throttle trips at a
fraction of its intended threshold.

The warm-up check cannot answer this: it submits a deliberately non-marketable
order and cancels it, so no fill is ever produced. This probe submits a genuinely
marketable order, observes the fill, records the raw broker response, and flattens.

DEVELOPMENT ACCOUNT ONLY. Requires an open market.

    PYTHONPATH=src .venv/bin/python scripts/fill_probe.py --confirm
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
import uuid
from pathlib import Path

from agent.config import ET, load_env, profile
from agent.host import gates
from agent.host.rest import Rest

OUT = Path(".run/fill_probe.json")
TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected"}


def pick_structure(rest: Rest) -> tuple[list[dict], float, str]:
    """A 1-wide call spread just out of the money: cheap, defined risk, liquid."""
    spot = float(rest.stock_latest_trade("SPY")["p"])
    today = dt.datetime.now(ET).date()
    expiries = sorted({c["expiration_date"] for c in rest.contracts(
        "SPY", today.isoformat(), (today + dt.timedelta(days=8)).isoformat())})
    if not expiries:
        raise RuntimeError("no expiries listed")
    expiry = expiries[0]
    calls = {float(c["strike_price"]): c for c in rest.contracts("SPY", expiry, expiry)
             if c["type"] == "call"}
    lo = min((k for k in calls if k >= spot + 2), default=None)
    if lo is None or (lo + 1) not in calls:
        raise RuntimeError("no 1-wide pair above spot")
    long_c, short_c = calls[lo], calls[lo + 1]
    quotes = rest.option_quotes([long_c["symbol"], short_c["symbol"]])
    for c in (long_c, short_c):
        q = quotes.get(c["symbol"])
        if not q or float(q.get("bp", 0) or 0) <= 0:
            raise RuntimeError(f"{c['symbol']} has no usable two-sided quote")
    # marketable: buy the long at its ask, sell the short at its bid, then pay a
    # little more so it crosses rather than resting
    net = float(quotes[long_c["symbol"]]["ap"]) - float(quotes[short_c["symbol"]]["bp"])
    legs = [{"symbol": long_c["symbol"], "ratio_qty": "1", "side": "buy",
             "position_intent": "buy_to_open"},
            {"symbol": short_c["symbol"], "ratio_qty": "1", "side": "sell",
             "position_intent": "sell_to_open"}]
    return legs, round(net + 0.05, 2), expiry


def wait_filled(rest: Rest, order_id: str, seconds: int = 60) -> dict:
    deadline = time.monotonic() + seconds
    order = {}
    while time.monotonic() < deadline:
        order = rest.order(order_id)
        if str(order.get("status", "")).lower() in TERMINAL:
            return order
        time.sleep(2)
    return order


def verdict(order: dict, submitted_qty: int) -> dict:
    parent_filled = float(order.get("filled_qty") or 0)
    legs = order.get("legs") or []
    leg_rows = [{"symbol": l.get("symbol"), "ratio_qty": l.get("ratio_qty"),
                 "qty": l.get("qty"), "filled_qty": l.get("filled_qty"),
                 "side": l.get("side"), "position_intent": l.get("position_intent"),
                 "filled_avg_price": l.get("filled_avg_price")} for l in legs]
    n_legs = len(legs) or 2
    ratios = [int(float(l.get("ratio_qty") or 1)) for l in legs] or [1] * n_legs
    ratio_total = sum(ratios)
    leg_units = [float(l["filled_qty"]) / ratio
                 for l, ratio in zip(legs, ratios)
                 if l.get("filled_qty") is not None and ratio > 0]
    completed_from_legs = min(leg_units) if len(leg_units) == len(legs) and legs else None
    if parent_filled == submitted_qty:
        denom = "spreads"
        impact = "realised_pnl is correct as written"
    elif parent_filled == submitted_qty * ratio_total:
        denom = "leg-contracts"
        impact = (f"parent filled_qty aggregates {ratio_total} contracts per structure; "
                  "derive structure units from each leg's filled_qty / ratio_qty")
    else:
        denom = "unrecognised"
        impact = (f"parent filled_qty={parent_filled} against submitted qty="
                  f"{submitted_qty} over {n_legs} legs with total ratio {ratio_total}; "
                  "inspect the raw response")
    return {"parent_qty": order.get("qty"), "parent_filled_qty": order.get("filled_qty"),
            "parent_filled_avg_price": order.get("filled_avg_price"),
            "submitted_qty": submitted_qty, "n_legs": n_legs,
            "total_ratio_qty": ratio_total,
            "completed_structure_qty_from_legs": completed_from_legs, "legs": leg_rows,
            "denomination": denom, "impact": impact}


def cancel_if_live(rest: Rest, order_id: str, latest: dict | None = None) -> dict:
    """Cancel a nonterminal order and return the broker's latest state."""
    latest = latest or rest.order(order_id)
    if str(latest.get("status", "")).lower() not in TERMINAL:
        rest.cancel(order_id)
        latest = wait_filled(rest, order_id, seconds=10)
    return latest


def marketable_mleg_limit(legs: list[dict], quotes: dict[str, dict]) -> float:
    """Cross the far side, then move five cents further in the fill direction."""
    net = sum((1 if leg["side"] == "buy" else -1)
              * int(leg.get("ratio_qty") or 1)
              * float(quotes[leg["symbol"]]["ap" if leg["side"] == "buy" else "bp"])
              for leg in legs)
    return round(net + 0.05, 2)


def closing_order_from_positions(positions: list[dict], symbols: set[str]
                                 ) -> tuple[list[dict], int]:
    """Build reduced closing ratios from actual broker positions, shorts first."""
    held = [p for p in positions if str(p.get("symbol")) in symbols
            and abs(float(p.get("qty") or 0)) > 0]
    if not held:
        return [], 0
    quantities = [int(abs(float(p["qty"]))) for p in held]
    if any(q <= 0 for q in quantities):
        return [], 0
    units = math.gcd(*quantities)
    closing = []
    for p, qty in zip(held, quantities):
        was_long = str(p.get("side")) == "long"
        closing.append({"symbol": str(p["symbol"]),
                        "ratio_qty": str(qty // units),
                        "side": "sell" if was_long else "buy",
                        "position_intent": "sell_to_close" if was_long
                        else "buy_to_close"})
    closing.sort(key=lambda leg: leg["position_intent"] != "buy_to_close")
    return closing, units


def flatten_probe_positions(rest: Rest, symbols: set[str], seconds: int = 60) -> dict | None:
    """Close whatever the probe actually filled; never infer quantity from the parent."""
    closing, qty = closing_order_from_positions(rest.positions(), symbols)
    if not closing:
        return None
    quotes = rest.option_quotes([leg["symbol"] for leg in closing])
    missing = [leg["symbol"] for leg in closing if leg["symbol"] not in quotes]
    if missing:
        raise RuntimeError(f"cannot flatten probe; missing quotes for {missing}")
    coid = "probex" + uuid.uuid4().hex[:25]
    if len(closing) == 1:
        leg = closing[0]
        quote = quotes[leg["symbol"]]
        price = (float(quote["ap"]) + 0.05 if leg["side"] == "buy"
                 else max(float(quote["bp"]) - 0.05, 0.01))
        order = rest.submit_single(leg["symbol"], qty, leg["side"],
                                   leg["position_intent"], round(price, 2), coid)
    else:
        order = rest.submit_mleg(closing, qty, marketable_mleg_limit(closing, quotes), coid)
    oid = str(order["id"])
    final = wait_filled(rest, oid, seconds=seconds)
    return cancel_if_live(rest, oid, final)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="required: this submits a real order that is meant to fill")
    ap.add_argument("--qty", type=int, default=1)
    args = ap.parse_args()
    if args.qty < 1:
        ap.error("--qty must be at least 1")

    load_env()
    prof = profile("dev")                      # never the competition account
    rest = Rest(prof)
    account = rest.account()
    now = dt.datetime.now(ET)
    clock = rest.clock()

    print(f"=== fill probe  {now:%a %Y-%m-%d %H:%M:%S ET} ===")
    print(f"    account {account['account_number']}  market_open={clock.get('is_open')}")
    identity = gates.g_account_identity(account, "dev", prof.expected_account_id, now=now)
    if not identity.passed:
        raise PermissionError(identity.reason)
    tradable = gates.g_account_tradable(account)
    if not tradable.passed:
        raise PermissionError(tradable.reason)
    if not clock.get("is_open"):
        print("\n!! the market is closed; a marketable order cannot fill.")
        print("!! run this in the first minutes of a session.")
        return
    if not args.confirm:
        print("\nthis submits a real, marketable order on the development account "
              "and then flattens it.\nre-run with --confirm.")
        return

    legs, limit, expiry = pick_structure(rest)
    print(f"\nsubmitting {args.qty}x SPY {expiry} call spread, marketable limit {limit}")
    for l in legs:
        print(f"    {l['side']:4} {l['symbol']}  ratio {l['ratio_qty']}")

    oid: str | None = None
    final: dict = {}
    symbols = {leg["symbol"] for leg in legs}
    preexisting = [p for p in rest.positions() if str(p.get("symbol")) in symbols]
    if preexisting:
        raise RuntimeError("probe contracts already have positions; refusing to mix "
                           "probe cleanup with pre-existing exposure")
    if rest.orders("open"):
        raise RuntimeError("development account has open orders; cancel them before "
                           "running the fill probe")
    try:
        coid = "probe" + uuid.uuid4().hex[:26]
        order = rest.submit_mleg(legs, args.qty, limit, coid)
        oid = str(order["id"])
        print(f"    order {oid}")

        final = wait_filled(rest, oid)
        final = cancel_if_live(rest, oid, final)

        # Persist the untouched broker payload first. Derived fields are added only
        # after the evidence needed to revisit the decision is safely on disk.
        OUT.parent.mkdir(parents=True, exist_ok=True)
        capture = {"measured_at": now.isoformat(), "raw_order": final}
        OUT.write_text(json.dumps(capture, indent=2, default=str))
        result = verdict(final, args.qty)
        capture.update(result)
        OUT.write_text(json.dumps(capture, indent=2, default=str))

        print(f"\nstatus {final.get('status')}")
        print(f"  parent: qty={result['parent_qty']} filled_qty="
              f"{result['parent_filled_qty']} avg={result['parent_filled_avg_price']}")
        for leg in result["legs"]:
            print(f"  leg   : {leg['symbol']} ratio={leg['ratio_qty']} qty={leg['qty']} "
                  f"filled={leg['filled_qty']} avg={leg['filled_avg_price']}")
        print(f"\nDENOMINATION: {result['denomination']}")
        print(f"  {result['impact']}")
        print(f"\nraw response written to {OUT}")
    finally:
        if oid:
            try:
                final = cancel_if_live(rest, oid, final or None)
            except Exception as exc:
                print(f"\n!! could not confirm entry cancellation: {exc}")
        print("\nflattening any probe position reported by the broker")
        try:
            done = flatten_probe_positions(rest, symbols)
            if done:
                print(f"    close order {done.get('id')} -> {done.get('status')}")
        except Exception as exc:
            print(f"    !! automatic flatten failed: {exc}")

        positions_left = [p for p in rest.positions() if str(p.get("symbol")) in symbols]
        open_orders = rest.orders("open")
        print(f"\nprobe positions remaining: {len(positions_left)}")
        print(f"open orders remaining: {len(open_orders)}")
        if positions_left or open_orders:
            print("    !! PROBE CLEANUP INCOMPLETE -- FLATTEN/CANCEL MANUALLY")


if __name__ == "__main__":
    main()
