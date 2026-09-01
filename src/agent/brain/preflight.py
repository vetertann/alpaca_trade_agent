"""Deterministic preflight collector.

What the system needs to look at is the same every cycle, so it is collected
deterministically and the model's first token goes to forming a hypothesis rather
than to writing data-fetching boilerplate. The bundle is a snapshot of warm local
state plus a few on-demand pulls -- a digest, not a dump.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json

from agent.config import ET, WINDOW_CLOSE
from agent.brain import scheduled_events
from agent.host.rest import Rest
from agent.host.series import RollingSeries
from agent.host.thesis_store import ThesisStore
from agent.quant import bs, score_horizon, vol as volmod


def _directional_contexts(series: RollingSeries, symbols, now: dt.datetime) -> dict:
    """Keep lightweight test/offline series implementations safely explicit."""
    if hasattr(series, "directional_contexts"):
        return series.directional_contexts(symbols, now)
    return {
        str(symbol).upper(): {
            "symbol": str(symbol).upper(),
            "source": "streamed equity quote midpoints",
            "observed_at_et": None,
            "sample_count": 0,
            "sample_coverage_minutes": 0.0,
            "classification": "insufficient_data",
            "strength": "none",
            "classification_basis": ["directional series unavailable"],
        }
        for symbol in symbols
    }


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


def live_trigger_universe(rest: Rest, series: RollingSeries, previous: dict,
                          expiries: list[str], now: dt.datetime,
                          *, refresh_iv: bool = False) -> dict:
    """Refresh the cheap Tier-1 inputs without rebuilding a full preflight.

    Spot and intraday volatility come from the continuous stream. Short-dated IV
    is refreshed on a slower caller-controlled cadence because it requires option
    quote requests. Daily regime fields remain those of the last full cycle.
    """
    out = {symbol: dict(row) for symbol, row in (previous or {}).items()}
    symbols = set(out) | {"SPY", "QQQ", "IWM"}
    directional = _directional_contexts(series, symbols, now)
    for symbol in symbols:
        row = out.setdefault(symbol, {})
        spot = series.last(symbol)
        if spot is None:
            try:
                spot = float(rest.stock_latest_trade(symbol)["p"])
            except Exception:
                continue
        row["spot"] = round(spot, 4)
        rng = series.session_range(symbol)
        if rng:
            row["session_low"], row["session_high"] = round(rng[0], 4), round(rng[1], 4)
        intraday_rv = series.realized_vol(symbol)
        row["intraday_realized_vol"] = (
            round(intraday_rv, 4) if intraday_rv is not None else None)
        row["directional_context"] = directional.get(symbol)
        if refresh_iv and symbol in ("SPY", "QQQ"):
            iv_by_expiry = {}
            for expiry in expiries[:2]:
                value, found = _atm_iv(rest, symbol, spot, [expiry], now)
                if value is not None and found:
                    iv_by_expiry[found] = value
            if iv_by_expiry:
                ordered = list(iv_by_expiry)
                front, nxt = ordered[0], ordered[1] if len(ordered) > 1 else None
                front_iv = iv_by_expiry[front]
                next_iv = iv_by_expiry.get(nxt) if nxt else None
                row.update({
                    "iv_atm": round(front_iv, 4), "iv_expiry": front,
                    "atm_iv_by_expiry": {key: round(value, 4)
                                         for key, value in iv_by_expiry.items()},
                    "front_expiry": front, "front_iv": round(front_iv, 4),
                    "next_expiry": nxt,
                    "next_iv": round(next_iv, 4) if next_iv is not None else None,
                    "absolute_slope": (round(front_iv - next_iv, 4)
                                       if next_iv is not None else None),
                    "relative_slope": (round(front_iv / next_iv - 1, 4)
                                       if next_iv else None),
                    "iv_term_observed_at": now.isoformat(timespec="seconds"),
                })
                rv = row.get("realized_vol")
                row["iv_rv_ratio"] = round(front_iv / float(rv), 3) if rv else None
                row["iv_intraday_rv_ratio"] = (
                    round(front_iv / intraday_rv, 3) if intraday_rv else None)
                windows = row.get("realized_vol_by_window") or {}
                row["iv_rv_by_window"] = {
                    key: round(front_iv / float(value), 3)
                    for key, value in windows.items() if value
                }
    return out


def build(rest: Rest, series: RollingSeries, theses: ThesisStore, *,
          trigger: dict, universe: list[str], expiries: list[str],
          account: dict, previous: dict | None = None,
          now: dt.datetime | None = None, trading_day: bool = True,
          history: list[dict] | None = None,
          blocked: list[dict] | None = None,
          execution_control: dict | None = None,
          starting_equity: float | None = None,
          runtime_state_age_seconds: float | None = None,
          active_thesis_ids: set[str] | None = None,
          positions: list[dict] | None = None,
          portfolio: dict | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(ET)

    uni: dict[str, dict] = {}
    directional = _directional_contexts(
        series, set(universe) | {"SPY", "QQQ", "IWM"}, now)
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
        iv_by_expiry: dict[str, float] = {}
        if sym in ("SPY", "QQQ"):
            for expiry in expiries[:2]:
                value, found_expiry = _atm_iv(rest, sym, spot, [expiry], now)
                if value is not None and found_expiry:
                    iv_by_expiry[found_expiry] = value
        iv_exp = next(iter(iv_by_expiry), None)
        iv = iv_by_expiry.get(iv_exp) if iv_exp else None
        term_expiries = list(iv_by_expiry)
        front_expiry = term_expiries[0] if term_expiries else None
        next_expiry = term_expiries[1] if len(term_expiries) > 1 else None
        front_iv = iv_by_expiry.get(front_expiry) if front_expiry else None
        next_iv = iv_by_expiry.get(next_expiry) if next_expiry else None
        rng = series.session_range(sym)
        uni[sym] = {
            "spot": round(spot, 2),
            "session_low": round(rng[0], 2) if rng else None,
            "session_high": round(rng[1], 2) if rng else None,
            "realized_vol": round(rv, 4) if rv is not None else None,
            "realized_vol_source": rv_source,
            "intraday_realized_vol": (round(intraday_rv, 4)
                                        if intraday_rv is not None else None),
            # Multi-horizon price direction, explicitly sourced from streamed
            # quote midpoints.  It describes observed path and never masquerades
            # as order flow, volume, or a return forecast.
            "directional_context": directional.get(sym),
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
            # Observables only.  Four sessions do not justify inventing a host-side
            # event-blackout threshold before seeing the live short-dated shape.
            "atm_iv_by_expiry": {e: round(v, 4) for e, v in iv_by_expiry.items()},
            "front_expiry": front_expiry,
            "front_iv": round(front_iv, 4) if front_iv is not None else None,
            "next_expiry": next_expiry,
            "next_iv": round(next_iv, 4) if next_iv is not None else None,
            "absolute_slope": (round(front_iv - next_iv, 4)
                               if front_iv is not None and next_iv is not None else None),
            "relative_slope": (round(front_iv / next_iv - 1, 4)
                               if front_iv is not None and next_iv else None),
            "iv_term_observed_at": now.isoformat(timespec="seconds"),
        }

    positions = rest.positions() if positions is None else positions
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
            "official_equity_mark_at": WINDOW_CLOSE.isoformat(timespec="seconds"),
            "trading_days_to_equity_mark": round(
                score_horizon.trading_days_between(et, WINDOW_CLOSE), 6),
            "listed_option_expiry_count": len(expiries),
            "furthest_listed_option_expiry": expiries[-1] if expiries else None,
            "expiry_eligibility": (
                "all active broker-listed expiries; value at earlier of expiry "
                "and official equity mark"),
            "runtime_state_age_seconds": runtime_state_age_seconds,
        },
        "scheduled_events": scheduled_events.context(now),
        "account": {
            "equity": float(account.get("equity", 0)),
            "starting_equity": starting_equity,
            "cash": float(account.get("cash", 0)),
            "options_buying_power": float(account.get("options_buying_power", 0) or 0),
            "options_level": account.get("options_trading_level"),
        },
        "book": book,
        # Structure-aware, executable portfolio state. Unlike `book`, this joins
        # broker legs to the durable ledger and names the id accepted by
        # trading.close(). Recent trajectories are bounded by the host.
        "portfolio": portfolio or {"structures": [], "structure_count": 0},
        # Thesis text is rationale, never evidence that exposure exists.  When the
        # reconciler supplies active ids, only broker-backed structures enter the
        # model context; canceled drafts remain available in the audit ledger but
        # cannot contaminate portfolio reasoning.
        "theses": [t.to_json() for t in theses.list("open")
                   if active_thesis_ids is None or t.thesis_id in active_thesis_ids],
        # What this agent already decided. Outcomes and gate refusals only -- prior
        # reasoning is deliberately withheld, because feeding a model its own
        # argument invites it to defend the position rather than re-examine it.
        "recent_cycles": (history or [])[-8:],
        "closed_theses": theses.outcomes(6),
        "blocked_structures": (blocked or [])[-8:],
        "execution_control": execution_control or {
            "entries_frozen": False, "latched": False, "blockers": []},
        "universe": uni,
        "expiries": expiries,
    }
    bundle["diff"] = _diff(previous, bundle)
    bundle["bundle_hash"] = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return bundle


def refresh_for_confirmation(rest: Rest, series: RollingSeries, bundle: dict, *,
                             account: dict, positions: list[dict], portfolio: dict,
                             expiries: list[str], now: dt.datetime,
                             trading_day: bool = True) -> dict:
    """Refresh mutable state before a staged entry can be confirmed.

    Historical regimes do not need another expensive download a minute later;
    prices, IV, account state and the existing book do.
    """
    refreshed = copy.deepcopy(bundle)
    et = now.astimezone(ET)
    refreshed["clock"].update({
        "now_et": et.isoformat(timespec="seconds"),
        "session_state": _session_state(et, trading_day),
        "minutes_to_close": _minutes_to(
            et, et.replace(hour=16, minute=0, second=0, microsecond=0)),
        "hours_to_window_close": round((WINDOW_CLOSE - et).total_seconds() / 3600, 1),
        "official_equity_mark_at": WINDOW_CLOSE.isoformat(timespec="seconds"),
        "trading_days_to_equity_mark": round(
            score_horizon.trading_days_between(et, WINDOW_CLOSE), 6),
        "listed_option_expiry_count": len(expiries),
        "furthest_listed_option_expiry": expiries[-1] if expiries else None,
        "expiry_eligibility": (
            "all active broker-listed expiries; value at earlier of expiry "
            "and official equity mark"),
    })
    refreshed["scheduled_events"] = scheduled_events.context(now)
    refreshed["account"].update({
        "equity": float(account.get("equity", 0)),
        "cash": float(account.get("cash", 0)),
        "options_buying_power": float(account.get("options_buying_power", 0) or 0),
        "options_level": account.get("options_trading_level"),
    })
    refreshed["book"] = [
        {"symbol": p["symbol"], "qty": p["qty"], "side": p["side"],
         "market_value": p.get("market_value"), "unrealized_pl": p.get("unrealized_pl"),
         "cost_basis": p.get("cost_basis")}
        for p in positions]
    refreshed["portfolio"] = portfolio
    refreshed["universe"] = live_trigger_universe(
        rest, series, refreshed.get("universe") or {}, expiries, now, refresh_iv=True)
    refreshed["diff"] = _diff(bundle, refreshed)
    refreshed["bundle_hash"] = hashlib.sha256(
        json.dumps({key: value for key, value in refreshed.items()
                    if key != "bundle_hash"}, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return refreshed


def _session_state(et: dt.datetime, trading_day: bool = True) -> str:
    if not trading_day or et.weekday() >= 5:
        return "CLOSED"
    t = et.time()
    if t < dt.time(9, 30) or t >= dt.time(16, 0):
        return "CLOSED"
    if t < dt.time(9, 45):
        return "WARM_UP"
    wind = dt.time(15, 0) if et.date() == WINDOW_CLOSE.date() else dt.time(15, 45)
    if t >= wind:
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
    old_portfolio = previous.get("portfolio") or {}
    new_portfolio = current.get("portfolio") or {}
    if old_portfolio.get("equity") and new_portfolio.get("equity"):
        out["equity_change"] = round(
            float(new_portfolio["equity"]) - float(old_portfolio["equity"]), 2)
    old_structures = {str(row.get("structure_id")): row
                      for row in old_portfolio.get("structures") or []}
    structure_changes = []
    for row in new_portfolio.get("structures") or []:
        old = old_structures.get(str(row.get("structure_id")))
        if not old:
            continue
        before = old.get("broker_unrealized_pl", old.get("unrealized_pl"))
        after = row.get("broker_unrealized_pl", row.get("unrealized_pl"))
        if before is not None and after is not None:
            structure_changes.append({
                "structure_id": row.get("structure_id"),
                "unrealized_change": round(float(after) - float(before), 2),
                "stop_progress": row.get("stop_progress"),
            })
    if structure_changes:
        out["structure_changes"] = structure_changes
    return out
