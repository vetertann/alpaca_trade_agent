"""Pre-open calibration.

Two parameters are guesses until the market prices them:

* `max_spread_pct_of_mid` -- the liquidity gate. Set from measurement, not from a
  number chosen in advance. Closing quotes are wider than intraday, so a threshold
  fitted on Friday's close rejects candidates that are perfectly tradeable at 09:45.
* the `iv/rv` reading -- the central edge signal. It is computed entirely from our
  own formula on both sides, so what matters is where it sits against its own
  history, not against remembered market levels.

Run at 09:35 ET on a session day, before the first entry.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as stats
from pathlib import Path

from agent.config import ET, load_env, profile
from agent.host.rest import Rest
from agent.quant import bs, vol


def spread_profile(rest: Rest, underlyings: list[str], expiries: list[str],
                   band: float = 8.0) -> dict:
    """The spread distribution over contracts we would actually consider."""
    rows: list[dict] = []
    for u in underlyings:
        spot = float(rest.stock_latest_trade(u)["p"])
        for exp in expiries:
            cs = [c for c in rest.contracts(u, exp, exp)
                  if abs(float(c["strike_price"]) - spot) <= band]
            if not cs:
                continue
            quotes = rest.option_quotes([c["symbol"] for c in cs])
            for c in cs:
                q = quotes.get(c["symbol"])
                if not q:
                    continue
                bid, ask = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
                if bid <= 0 or ask <= 0 or ask < bid:
                    continue
                mid = (bid + ask) / 2
                rows.append({"underlying": u, "expiry": exp,
                             "strike": float(c["strike_price"]),
                             "moneyness": abs(float(c["strike_price"]) - spot) / spot,
                             "pct": (ask - bid) / mid * 100})
    if not rows:
        return {"error": "no two-sided quotes in band"}

    pcts = sorted(r["pct"] for r in rows)
    def q(p): return pcts[min(int(len(pcts) * p), len(pcts) - 1)]
    atm = sorted(r["pct"] for r in rows if r["moneyness"] <= 0.005)
    return {"n": len(rows), "min": round(pcts[0], 2), "p25": round(q(0.25), 2),
            "median": round(stats.median(pcts), 2), "p75": round(q(0.75), 2),
            "p90": round(q(0.90), 2), "max": round(pcts[-1], 2),
            "atm_median": round(stats.median(atm), 2) if atm else None,
            "atm_p90": round(sorted(atm)[int(len(atm) * 0.9)], 2) if atm else None}


def recommend_threshold(prof: dict) -> tuple[float, str]:
    """Admit the near-the-money band comfortably; exclude the illiquid tail.

    The p90 of at-the-money spreads plus headroom keeps the contracts the strategy
    is built around, while the long tail of far strikes still fails the gate.
    """
    if "error" in prof:
        return 12.0, "no data; leaving the parameter unchanged"
    base = prof.get("atm_p90") or prof.get("p75") or 12.0
    rec = round(min(max(base * 1.4, 3.0), 25.0), 1)
    return rec, (f"atm p90 {prof.get('atm_p90')}% x1.4 headroom -> {rec}%; "
                 f"chain median {prof['median']}%, p90 {prof['p90']}%")


def vol_state(rest: Rest, symbols: list[str], expiries: list[str],
              now: dt.datetime) -> dict:
    out = {}
    for sym in symbols:
        spot = float(rest.stock_latest_trade(sym)["p"])
        end = (now - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
        start = (now - dt.timedelta(days=200)).date().isoformat()
        bars = rest.stock_bars(sym, "1Day", start, end)
        rv = vol.ewma_from_bars(bars)
        closes = [float(b["c"]) for b in bars if b.get("c")]

        iv = None
        for exp in expiries:
            cs = [c for c in rest.contracts(sym, exp, exp)
                  if abs(float(c["strike_price"]) - spot) <= 2]
            if not cs:
                continue
            quotes = rest.option_quotes([c["symbol"] for c in cs])
            t = bs.year_fraction(now, dt.datetime.fromisoformat(exp).replace(
                hour=16, tzinfo=ET))
            ivs = []
            for c in cs:
                q = quotes.get(c["symbol"])
                if not q or float(q.get("bp", 0) or 0) <= 0:
                    continue
                m = (float(q["bp"]) + float(q["ap"])) / 2
                v = bs.implied_vol(m, spot, float(c["strike_price"]), t, c["type"])
                if v:
                    ivs.append(v)
            if ivs:
                iv = sum(ivs) / len(ivs)
                break

        # where the current ratio sits in its own history, under our own formula
        hist = []
        for i in range(30, len(closes) - 20):
            r = vol.realized_from_closes(closes[i - 30:i], vol.TRADING_DAYS)
            f = vol.realized_from_closes(closes[i:i + 20], vol.TRADING_DAYS)
            if r and f:
                hist.append(f / r)
        ratio = (iv / rv) if (iv and rv) else None
        pct = (round(sum(1 for h in hist if h < ratio) / len(hist) * 100, 1)
               if ratio and hist else None)
        out[sym] = {"spot": round(spot, 2),
                    "realized_ewma": round(rv, 4) if rv else None,
                    "iv_atm": round(iv, 4) if iv else None,
                    "iv_rv_ratio": round(ratio, 3) if ratio else None,
                    "ratio_percentile_own_history": pct,
                    "reading": _read(ratio)}
    return out


def _read(ratio: float | None) -> str:
    if ratio is None:
        return "unavailable"
    if ratio < 0.85:
        return "implied below realized -- long premium is the cheap side"
    if ratio > 1.20:
        return "implied above realized -- defined-risk premium selling is the cheap side"
    return "implied and realized close -- no clear volatility edge"


def main() -> None:
    ap = argparse.ArgumentParser(description="pre-open calibration")
    ap.add_argument("--profile", required=True, choices=["competition", "dev"])
    ap.add_argument("--out", default=".run/calibration.json")
    args = ap.parse_args()

    load_env()
    rest = Rest(profile(args.profile))
    now = dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(ET)
    clock = rest.clock()
    live = bool(clock.get("is_open"))

    today = et.date()
    expiries = sorted({c["expiration_date"] for c in rest.contracts(
        "SPY", today.isoformat(), (today + dt.timedelta(days=8)).isoformat())})[:2]

    print(f"=== calibration {et:%a %Y-%m-%d %H:%M ET} ===")
    print(f"market open: {live}"
          + ("" if live else "   <- quotes are stale; rerun during the session"))
    print(f"expiries: {expiries}\n")

    prof = spread_profile(rest, ["SPY", "QQQ"], expiries)
    print("spread distribution, +/-8 strikes:")
    for k in ("n", "min", "p25", "median", "p75", "p90", "max", "atm_median", "atm_p90"):
        if k in prof:
            print(f"  {k:12} {prof[k]}")
    rec, why = recommend_threshold(prof)
    print(f"\n  recommended max_spread_pct_of_mid = {rec}")
    print(f"  {why}")

    vs = vol_state(rest, ["SPY", "QQQ"], expiries, now)
    print("\nvolatility state:")
    for sym, v in vs.items():
        print(f"  {sym}: spot {v['spot']}  iv {v['iv_atm']}  rv {v['realized_ewma']}  "
              f"iv/rv {v['iv_rv_ratio']}  pct {v['ratio_percentile_own_history']}")
        print(f"       {v['reading']}")

    payload = {"at": et.isoformat(), "market_open": live, "expiries": expiries,
               "spread_profile": prof, "recommended_max_spread_pct": rec,
               "rationale": why, "vol_state": vs}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"\nwritten to {args.out}")
    if not live:
        print("NOTE: run again after 09:35 ET on a session day before the first entry.")


if __name__ == "__main__":
    main()
