"""Deterministic preflight collector.

What the system needs to look at is the same every cycle, so it is collected
deterministically and the model's first token goes to forming a hypothesis rather
than to writing data-fetching boilerplate. The bundle is a snapshot of warm local
state plus a few on-demand pulls -- a digest, not a dump.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from agent.config import ET, WINDOW_CLOSE
from agent.host.rest import Rest
from agent.host.series import RollingSeries
from agent.host.thesis_store import ThesisStore
from agent.quant import bs, vol as volmod


def _atm_iv(rest: Rest, underlying: str, spot: float, expiries: list[str],
            now: dt.datetime) -> tuple[float | None, str | None]:
    for exp in expiries:
        cs = [c for c in rest.contracts(underlying, exp, exp)
              if abs(float(c["strike_price"]) - spot) <= 2]
        if not cs:
            continue
        quotes = rest.option_quotes([c["symbol"] for c in cs])
        expiry_dt = dt.datetime.fromisoformat(exp).replace(hour=16, tzinfo=ET)
        t = bs.year_fraction(now, expiry_dt)
        ivs = []
        for c in cs:
            q = quotes.get(c["symbol"])
            if not q or float(q.get("bp", 0) or 0) <= 0:
                continue
            mid = (float(q["bp"]) + float(q["ap"])) / 2
            iv = bs.implied_vol(mid, spot, float(c["strike_price"]), t, c["type"])
            if iv:
                ivs.append(iv)
        if ivs:
            return sum(ivs) / len(ivs), exp
    return None, None


def build(rest: Rest, series: RollingSeries, theses: ThesisStore, *,
          trigger: dict, universe: list[str], expiries: list[str],
          account: dict, previous: dict | None = None,
          now: dt.datetime | None = None, trading_day: bool = True,
          history: list[dict] | None = None,
          blocked: list[dict] | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(ET)

    uni: dict[str, dict] = {}
    for sym in universe:
        spot = series.last(sym)
        if spot is None:
            try:
                spot = float(rest.stock_latest_trade(sym)["p"])
            except Exception:
                continue
        intraday_rv = series.realized_vol(sym)
        daily_ewma = None
        rv_by_window: dict[str, float] = {}
        # Daily regime estimates must not disappear once the live minute stream is
        # warm.  The old fallback-only path changed the meaning of `realized_vol`
        # during the session and emptied the multi-window fields exactly when the
        # agent was allowed to trade.
        try:
            end = (now - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
            start = (now - dt.timedelta(days=120)).date().isoformat()
            bars = rest.stock_bars(sym, "1Day", start, end)
            daily_ewma = volmod.ewma_from_bars(bars)
            rv_by_window = {f"rv{w}": round(v, 4) for w in (5, 10, 20, 60)
                            if (v := volmod.realized_from_bars(bars, w)) is not None}
        except Exception:
            pass
        rv = daily_ewma if daily_ewma is not None else intraday_rv
        rv_source = ("daily_ewma" if daily_ewma is not None else
                     "intraday" if intraday_rv is not None else "unavailable")
        iv, iv_exp = (_atm_iv(rest, sym, spot, expiries, now)
                      if sym in ("SPY", "QQQ") else (None, None))
        rng = series.session_range(sym)
        uni[sym] = {
            "spot": round(spot, 2),
            "session_low": round(rng[0], 2) if rng else None,
            "session_high": round(rng[1], 2) if rng else None,
            "realized_vol": round(rv, 4) if rv is not None else None,
            "realized_vol_source": rv_source,
            "intraday_realized_vol": (round(intraday_rv, 4)
                                        if intraday_rv is not None else None),
            # One lookback is not a signal. On 2026-08-29 SPY implied read cheap
            # against 20 and 60 days and rich against 5, so a cycle trusting the
            # headline ratio alone would have been trading a choice of window.
            "realized_vol_by_window": rv_by_window,
            "iv_rv_by_window": {k: round(iv / v, 3) for k, v in rv_by_window.items()
                                if iv and v} if iv else {},
            "iv_intraday_rv_ratio": (round(iv / intraday_rv, 3)
                                      if iv and intraday_rv else None),
            "iv_atm": round(iv, 4) if iv is not None else None,
            "iv_rv_ratio": round(iv / rv, 3) if (iv and rv) else None,
            "iv_expiry": iv_exp,
        }

    positions = rest.positions()
    book = [{"symbol": p["symbol"], "qty": p["qty"], "side": p["side"],
             "market_value": p.get("market_value"),
             "unrealized_pl": p.get("unrealized_pl"),
             "cost_basis": p.get("cost_basis")} for p in positions]

    bundle = {
        "trigger": trigger,
        "clock": {
            "now_et": et.isoformat(timespec="seconds"),
            "session_state": _session_state(et, trading_day),
            "minutes_to_close": _minutes_to(et, et.replace(hour=16, minute=0, second=0)),
            "hours_to_window_close": round(
                (WINDOW_CLOSE - et).total_seconds() / 3600, 1),
        },
        "account": {
            "equity": float(account.get("equity", 0)),
            "cash": float(account.get("cash", 0)),
            "options_buying_power": float(account.get("options_buying_power", 0) or 0),
            "options_level": account.get("options_trading_level"),
        },
        "book": book,
        "theses": [t.to_json() for t in theses.list("open")],
        # What this agent already decided. Outcomes and gate refusals only -- prior
        # reasoning is deliberately withheld, because feeding a model its own
        # argument invites it to defend the position rather than re-examine it.
        "recent_cycles": (history or [])[-8:],
        "closed_theses": theses.outcomes(6),
        "blocked_structures": (blocked or [])[-8:],
        "universe": uni,
        "expiries": expiries,
    }
    bundle["diff"] = _diff(previous, bundle)
    bundle["bundle_hash"] = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return bundle


def _session_state(et: dt.datetime, trading_day: bool = True) -> str:
    if not trading_day or et.weekday() >= 5:
        return "CLOSED"
    t = et.time()
    if t < dt.time(9, 30) or t >= dt.time(16, 0):
        return "CLOSED"
    if t < dt.time(9, 45):
        return "WARM_UP"
    if t >= dt.time(15, 45):
        return "WINDING_DOWN"
    return "ACTIVE"


def _minutes_to(now: dt.datetime, target: dt.datetime) -> int:
    return max(int((target - now).total_seconds() // 60), 0)


def _diff(previous: dict | None, current: dict) -> dict:
    """What moved since the last cycle. Points the turn at novelty."""
    if not previous:
        return {"note": "first cycle of the session"}
    out: dict = {"since": previous.get("clock", {}).get("now_et")}
    for sym, cur in current["universe"].items():
        old = previous.get("universe", {}).get(sym)
        if not old or not old.get("spot") or not cur.get("spot"):
            continue
        entry = {"spot_move_pct": round((cur["spot"] / old["spot"] - 1) * 100, 3)}
        if old.get("iv_rv_ratio") and cur.get("iv_rv_ratio"):
            entry["iv_rv_change"] = round(cur["iv_rv_ratio"] - old["iv_rv_ratio"], 3)
        out[sym] = entry
    old_ids = {t["thesis_id"] for t in previous.get("theses", [])}
    new_ids = {t["thesis_id"] for t in current["theses"]}
    if new_ids - old_ids:
        out["theses_opened"] = sorted(new_ids - old_ids)
    if old_ids - new_ids:
        out["theses_closed"] = sorted(old_ids - new_ids)
    return out
