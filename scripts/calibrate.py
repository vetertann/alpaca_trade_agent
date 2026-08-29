#!/usr/bin/env python
"""Measure live option spreads and the volatility state, then recommend parameters.

Run this in the first minutes of a session, before the first entry. Closing quotes
are systematically wider than intraday ones, so a threshold picked from Friday's
close will be wrong in the direction that blocks every candidate.

    PYTHONPATH=src .venv/bin/python scripts/calibrate.py [--apply]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as stats
from pathlib import Path

from agent.config import ET, load_env, profile
from agent.host.rest import Rest
from agent.host.risk_params import DEFAULT as RP
from agent.quant import bs, vol

UNDERLYINGS = ("SPY", "QQQ", "IWM")
BANDS = {"atm": 3.0, "near": 8.0, "wing": 20.0}


def _band(strike: float, spot: float) -> str | None:
    d = abs(strike - spot)
    for name, width in BANDS.items():
        if d <= width:
            return name
    return None


def sample(rest: Rest, underlying: str, expiries: list[str]) -> dict:
    spot = float(rest.stock_latest_trade(underlying)["p"])
    rows: list[dict] = []
    for exp in expiries:
        contracts = [c for c in rest.contracts(underlying, exp, exp)
                     if _band(float(c["strike_price"]), spot)]
        if not contracts:
            continue
        quotes = rest.option_quotes([c["symbol"] for c in contracts])
        for c in contracts:
            q = quotes.get(c["symbol"])
            if not q:
                continue
            bid, ask = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
            mid = (bid + ask) / 2
            rows.append({"symbol": c["symbol"], "expiry": exp,
                         "strike": float(c["strike_price"]), "type": c["type"],
                         "bid": bid, "ask": ask, "mid": mid,
                         "band": _band(float(c["strike_price"]), spot),
                         "zero_bid": bid <= 0,
                         "spread_abs": round(ask - bid, 3),
                         "spread_pct": (ask - bid) / mid * 100 if mid > 0 else None})
    return {"underlying": underlying, "spot": spot, "rows": rows}


def spread_report(s: dict) -> dict:
    rows = [r for r in s["rows"] if not r["zero_bid"] and r["spread_pct"] is not None]
    zero = sum(1 for r in s["rows"] if r["zero_bid"])
    out = {"underlying": s["underlying"], "spot": round(s["spot"], 2),
           "quoted": len(s["rows"]), "zero_bid": zero, "usable": len(rows), "bands": {}}
    for band in BANDS:
        band_rows = [r for r in rows if r["band"] == band]
        vals = sorted(r["spread_pct"] for r in band_rows)
        abs_vals = sorted(r["spread_abs"] for r in band_rows)
        if not vals:
            continue
        out["bands"][band] = {
            "n": len(vals), "min": round(vals[0], 2),
            "p50": round(stats.median(vals), 2),
            "p90": round(vals[int(len(vals) * 0.9)], 2),
            "max": round(vals[-1], 2),
            "abs_p50": abs_vals[len(abs_vals) // 2],
            "abs_p90": abs_vals[int(len(abs_vals) * 0.9)]}
    return out


def recommend(reports: list[dict]) -> dict:
    """Threshold driven by the at-the-money band on the two traded ETFs.

    Percentage of mid is unstable for cheap contracts -- a five-cent spread is 50%
    of a ten-cent option and 1% of a five-dollar one -- so the outer edge of the
    near band inflates any percentile taken across it. The at-the-money band is
    what the strategy actually trades, so it sets the threshold, and an absolute
    allowance keeps genuinely cheap contracts from being rejected on arithmetic.
    """
    pct_pool, abs_pool = [], []
    for r in reports:
        if r["underlying"] not in ("SPY", "QQQ"):
            continue
        b = r["bands"].get("atm")
        if b:
            pct_pool.append(b["p90"])
            abs_pool.append(b["abs_p90"])
    if not pct_pool:
        return {"max_spread_pct_of_mid": RP.max_spread_pct_of_mid,
                "basis": "no usable sample; keeping the current value"}
    widest = max(pct_pool)
    value = max(3.0, min(round(widest * 1.5, 1), 25.0))
    return {"max_spread_pct_of_mid": value,
            "max_spread_abs": round(max(abs_pool) * 1.5, 2),
            "basis": f"1.5x the widest at-the-money p90 across SPY/QQQ ({widest}%); "
                     f"near/wing bands deliberately excluded because percentage of "
                     f"mid inflates on cheap contracts"}


def vol_state(rest: Rest, underlying: str, expiry: str, now: dt.datetime) -> dict:
    spot = float(rest.stock_latest_trade(underlying)["p"])
    end = (now - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
    start = (now - dt.timedelta(days=180)).date().isoformat()
    bars = rest.stock_bars(underlying, "1Day", start, end)
    rv20 = vol.realized_from_bars(bars, 20)
    ewma = vol.ewma_from_bars(bars)

    contracts = [c for c in rest.contracts(underlying, expiry, expiry)
                 if abs(float(c["strike_price"]) - spot) <= 2]
    quotes = rest.option_quotes([c["symbol"] for c in contracts])
    expiry_dt = dt.datetime.fromisoformat(expiry).replace(hour=16, tzinfo=ET)
    t = bs.year_fraction(now, expiry_dt)
    ivs = []
    for c in contracts:
        q = quotes.get(c["symbol"])
        if not q or float(q.get("bp", 0) or 0) <= 0:
            continue
        mid = (float(q["bp"]) + float(q["ap"])) / 2
        iv = bs.implied_vol(mid, spot, float(c["strike_price"]), t, c["type"])
        if iv:
            ivs.append(iv)
    iv_atm = stats.median(ivs) if ivs else None
    return {"underlying": underlying, "spot": round(spot, 2), "expiry": expiry,
            "dte_days": round(t * 365, 2), "bars": len(bars),
            "rv20": round(rv20, 4) if rv20 else None,
            "ewma": round(ewma, 4) if ewma else None,
            "iv_atm": round(iv_atm, 4) if iv_atm else None,
            "iv_rv_ratio": round(iv_atm / ewma, 3) if (iv_atm and ewma) else None,
            "n_iv": len(ivs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="dev", choices=["dev", "competition"])
    ap.add_argument("--apply", action="store_true",
                    help="write the recommendation to .run/calibration.json")
    args = ap.parse_args()

    load_env()
    rest = Rest(profile(args.profile))
    now = dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(ET)
    clock = rest.clock()
    live = bool(clock.get("is_open"))

    print(f"=== calibration  {et:%a %Y-%m-%d %H:%M:%S ET}  market_open={live} ===")
    if not live:
        print("!! market is CLOSED -- closing quotes are systematically wider than")
        print("!! intraday. Treat every number below as an upper bound and re-run")
        print("!! after 09:35 ET on a session day.\n")

    today = et.date()
    expiries = sorted({c["expiration_date"] for c in rest.contracts(
        "SPY", today.isoformat(), (today + dt.timedelta(days=8)).isoformat())})[:2]
    print(f"expiries under test: {expiries}\n")

    reports = []
    for u in UNDERLYINGS:
        rep = spread_report(sample(rest, u, expiries))
        reports.append(rep)
        print(f"{u}  spot {rep['spot']}  quoted {rep['quoted']}  "
              f"zero-bid {rep['zero_bid']}  usable {rep['usable']}")
        for band, b in rep["bands"].items():
            print(f"   {band:5} n={b['n']:4}  p50 {b['p50']:6.2f}%  p90 {b['p90']:6.2f}%  "
                  f"max {b['max']:7.2f}%   abs p50 ${b['abs_p50']:.2f} p90 ${b['abs_p90']:.2f}")
        if rep["quoted"]:
            zr = rep["zero_bid"] / rep["quoted"] * 100
            if zr > 10:
                print(f"   !! {zr:.0f}% of quoted contracts have a zero bid -- "
                      f"thin book, treat {u} as conditional")

    rec = recommend(reports)
    print(f"\nrecommended MAX_SPREAD_PCT_OF_MID = {rec['max_spread_pct_of_mid']}"
          f"   (current {RP.max_spread_pct_of_mid})")
    if rec.get("max_spread_abs"):
        print(f"recommended absolute allowance   = ${rec['max_spread_abs']}")
    print(f"  basis: {rec['basis']}")

    print("\n=== volatility state ===")
    states = []
    for u in ("SPY", "QQQ"):
        vs = vol_state(rest, u, expiries[-1], now)
        states.append(vs)
        print(f"  {u}  spot {vs['spot']}  dte {vs['dte_days']}d  "
              f"rv20 {vs['rv20']}  ewma {vs['ewma']}  iv_atm {vs['iv_atm']}  "
              f"iv/rv {vs['iv_rv_ratio']}  (n={vs['n_iv']})")
    for vs in states:
        r = vs["iv_rv_ratio"]
        if r is None:
            print(f"  {vs['underlying']}: no reading")
        elif r < 0.9:
            print(f"  {vs['underlying']}: implied below realized -- long premium favoured, "
                  f"if it survives all three measures")
        elif r > 1.1:
            print(f"  {vs['underlying']}: implied above realized -- defined-risk premium "
                  f"selling favoured")
        else:
            print(f"  {vs['underlying']}: implied near realized -- no volatility edge")

    if args.apply:
        out = {"measured_at": now.isoformat(), "market_open": live,
               "spread_reports": reports, "recommendation": rec, "vol_state": states}
        p = Path(".run/calibration.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2))
        print(f"\nwritten to {p}")
        if not live:
            print("(recorded, but do not trust it until re-run during a session)")


if __name__ == "__main__":
    main()
