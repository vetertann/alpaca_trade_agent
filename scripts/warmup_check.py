#!/usr/bin/env python
"""Warm-up protocol. Run 09:30-09:45 ET, before the first entry.

The market is closed all weekend, so this is the first and only contact with a
live tape before the scored window. Everything here can only be checked with a
market open. Order-path checks run on the DEV account so the competition account
stays untouched.

    PYTHONPATH=src .venv/bin/python scripts/warmup_check.py --order
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import statistics as stats
import time
import uuid

from agent.brain.loop import session_state
from agent.config import ET, load_env, profile
from agent.host import gates
from agent.host.execution import Executor
from agent.host.rest import Rest
from agent.host.risk_params import DEFAULT as RP
from agent.host.series import RollingSeries
from agent.host.streams import Handlers, StreamSet
from agent.types import Leg, TradeIntent

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


async def stream_delivery(prof, rest, seconds: int = 45) -> RollingSeries:
    """Connected is not the same as delivering. Only a live tape proves this."""
    series = RollingSeries()
    counts = {"equity": 0, "option": 0, "news": 0}

    def eq(s, b, a, w):
        counts["equity"] += 1
        if b > 0 and a > 0:
            series.observe(s, (b + a) / 2, w)

    def op(s, b, a, w):
        counts["option"] += 1
        if b > 0 and a > 0:
            series.observe(s, (b + a) / 2, w)

    spot = float(rest.stock_latest_trade("SPY")["p"])
    exps = sorted({c["expiration_date"] for c in rest.contracts(
        "SPY", dt.datetime.now(ET).date().isoformat(),
        (dt.datetime.now(ET).date() + dt.timedelta(days=8)).isoformat())})[:2]
    opts = [c["symbol"] for e in exps
            for c in rest.contracts("SPY", e, e)
            if abs(float(c["strike_price"]) - spot) <= 7][:150]

    ss = StreamSet(prof, Handlers(on_equity_quote=eq, on_option_quote=op,
                                  on_news=lambda a: counts.__setitem__("news", counts["news"] + 1),
                                  on_trade_update=lambda d: None,
                                  on_error=lambda n, e: print(f"    stream {n}: {e}")))
    await ss.start(["SPY", "QQQ", "IWM", "NVDA", "AAPL"], opts)
    print(f"  ... listening {seconds}s on {len(opts)} option symbols")
    await asyncio.sleep(seconds)
    st = ss.status()
    await ss.stop()

    check("streams connected", all(v["connected"] for v in st.values()),
          ", ".join(f"{k}={'up' if v['connected'] else 'DOWN'}" for k, v in st.items()))
    check("equity quotes flowing", counts["equity"] > 0,
          f"{counts['equity']} equity quotes in {seconds}s")
    check("option quotes flowing", counts["option"] > 0,
          f"{counts['option']} option quotes in {seconds}s "
          f"(this is the one a closed market cannot prove)")
    check("rolling series filling", bool(series.minute_closes("SPY")) or
          series.last("SPY") is not None,
          f"SPY last={series.last('SPY')}, "
          f"{len(series.minute_closes('SPY'))} minute closes")
    return series


def quote_freshness(rest: Rest) -> None:
    spot = float(rest.stock_latest_trade("SPY")["p"])
    today = dt.datetime.now(ET).date()
    exps = sorted({c["expiration_date"] for c in rest.contracts(
        "SPY", today.isoformat(), (today + dt.timedelta(days=8)).isoformat())})[:1]
    cs = [c for c in rest.contracts("SPY", exps[0], exps[0])
          if abs(float(c["strike_price"]) - spot) <= 5]
    q = rest.option_quotes([c["symbol"] for c in cs])
    now = dt.datetime.now(dt.timezone.utc)
    ages, valid, zero = [], 0, 0
    for c in cs:
        v = q.get(c["symbol"])
        if not v:
            continue
        if float(v.get("bp", 0) or 0) <= 0:
            zero += 1
        r = gates.g_quote_valid(c["symbol"], v, RP, now=now)
        valid += r.passed
        if v.get("t"):
            ages.append((now - dt.datetime.fromisoformat(
                str(v["t"]).replace("Z", "+00:00"))).total_seconds())
    check("quotes pass the validity gate", valid > len(cs) * 0.7,
          f"{valid}/{len(cs)} valid, {zero} zero-bid")
    if ages:
        check("quotes fresh within the staleness budget",
              stats.median(ages) < RP.max_quote_age_s,
              f"median age {stats.median(ages):.0f}s, max {max(ages):.0f}s "
              f"(budget {RP.max_quote_age_s:.0f}s)")


def spread_calibration(rest: Rest) -> None:
    spot = float(rest.stock_latest_trade("SPY")["p"])
    today = dt.datetime.now(ET).date()
    exps = sorted({c["expiration_date"] for c in rest.contracts(
        "SPY", today.isoformat(), (today + dt.timedelta(days=8)).isoformat())})[:1]
    cs = [c for c in rest.contracts("SPY", exps[0], exps[0])
          if abs(float(c["strike_price"]) - spot) <= 3]
    q = rest.option_quotes([c["symbol"] for c in cs])
    pcts = []
    for c in cs:
        v = q.get(c["symbol"])
        if not v:
            continue
        b, a = float(v.get("bp", 0) or 0), float(v.get("ap", 0) or 0)
        if b > 0 and a > 0:
            pcts.append((a - b) / ((a + b) / 2) * 100)
    if not pcts:
        check("live spread sample", False, "no usable at-the-money quotes")
        return
    pcts.sort()
    p90 = pcts[int(len(pcts) * 0.9)]
    check("live at-the-money spreads inside the gate", p90 <= RP.max_spread_pct_of_mid,
          f"p50 {stats.median(pcts):.2f}%  p90 {p90:.2f}%  "
          f"gate {RP.max_spread_pct_of_mid:.1f}%")
    if p90 > RP.max_spread_pct_of_mid:
        print(f"    -> raise max_spread_pct_of_mid to about {p90 * 1.5:.1f}")


def order_round_trip(rest: Rest) -> None:
    """Validate gates, then submit and cancel a non-marketable DEV order.

    The production executor deliberately prices at the executable far side. Using
    that price for a connectivity check could fill immediately, so the rehearsal
    submits the exact staged geometry at a one-cent debit and cancels it in a
    ``finally`` block. Production pricing itself is covered by executor tests.
    """
    spot = float(rest.stock_latest_trade("SPY")["p"])
    today = dt.datetime.now(ET).date()
    exps = sorted({c["expiration_date"] for c in rest.contracts(
        "SPY", today.isoformat(), (today + dt.timedelta(days=8)).isoformat())})
    if not exps:
        check("order round trip", False, "no expiries listed")
        return
    exp = exps[-1]
    cs = {float(c["strike_price"]): c for c in rest.contracts("SPY", exp, exp)
          if c["type"] == "call"}
    lo = min((k for k in cs if k > spot + 20), default=None)
    if lo is None or (lo + 5) not in cs:
        check("order round trip", False, "no far out-of-the-money 5-wide pair listed")
        return
    legs = (Leg(cs[lo]["symbol"], 1, "buy", "buy_to_open", lo, "call",
                dt.date.fromisoformat(exp)),
            Leg(cs[lo + 5]["symbol"], 1, "sell", "sell_to_open", lo + 5, "call",
                dt.date.fromisoformat(exp)))
    intent = TradeIntent("SPY", "vertical_call", legs, "th_warmup", 300.0)

    ex = Executor(rest, RP, "dev", mode="execute")
    acct = rest.account()
    kw = dict(equity=float(acct["equity"]), open_positions=[])
    staged = ex.materialise(intent, **kw)
    check("stage returns a checklist", staged.verified.qty >= 0,
          f"qty {staged.verified.qty} @ {staged.verified.limit_price:.2f}, "
          f"gates {'pass' if staged.passed else 'block'}")
    if not staged.passed or staged.verified.qty < 1:
        print("    (gates blocked it -- the submit path stays untested)")
        for g in staged.results:
            if not g.passed:
                print(f"      {g.render()}")
        return

    coid = "warmup-" + uuid.uuid4().hex[:24]
    api_legs = [{"symbol": l.symbol, "ratio_qty": str(l.ratio_qty), "side": l.side,
                 "position_intent": l.position_intent} for l in legs]
    order_id = None
    try:
        submitted = rest.submit_mleg(api_legs, 1, 0.01, coid)
        order_id = submitted["id"]
        check("order submitted to Alpaca", True, str(order_id))
        time.sleep(1)
        o = rest.order(order_id)
        check("order visible with our client_order_id",
              o.get("client_order_id") == coid,
              f"status={o.get('status')} legs={len(o.get('legs') or [])}")
    finally:
        if order_id:
            current = rest.order(order_id)
            if current.get("status") not in ("filled", "canceled", "expired", "rejected"):
                rest.cancel(order_id)
                time.sleep(1)
    after = rest.order(order_id)
    check("order cancels cleanly",
          after.get("status") in ("canceled", "pending_cancel", "filled"),
          f"status={after.get('status')}")
    check("no position left behind", not rest.positions(),
          f"{len(rest.positions())} positions on the dev account")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", action="store_true",
                    help="place and cancel a real order on the DEV account")
    ap.add_argument("--seconds", type=int, default=45)
    args = ap.parse_args()

    load_env()
    prof = profile("dev")
    rest = Rest(prof)
    now = dt.datetime.now(ET)
    clock = rest.clock()

    print(f"=== warm-up protocol  {now:%a %Y-%m-%d %H:%M:%S ET} ===")
    print(f"    market_open={clock.get('is_open')}  "
          f"session_state={session_state(now, bool(clock.get('is_open')))}\n")
    if not clock.get("is_open"):
        print("!! market is CLOSED. Stream-delivery and spread checks cannot pass;")
        print("!! the structural checks below still run.\n")

    print("1. stream delivery")
    asyncio.run(stream_delivery(prof, rest, args.seconds))
    print("\n2. quote validity and freshness")
    quote_freshness(rest)
    print("\n3. spread calibration against the live gate")
    spread_calibration(rest)
    if args.order:
        print("\n4. order round trip (DEV account)")
        order_round_trip(rest)
    else:
        print("\n4. order round trip -- skipped, pass --order to run it")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== {passed}/{len(RESULTS)} checks passed ===")
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print("failed: " + ", ".join(failed))
        print("\nGO/NO-GO: do not start the competition agent until these pass.")
    else:
        print("\nGO/NO-GO: clear to start the competition agent.")


if __name__ == "__main__":
    main()
