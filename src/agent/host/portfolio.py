"""Live, structure-aware portfolio observations.

The broker supplies leg positions and marks while the execution ledger supplies
membership and entry cash flows.  This module combines them into the compact
state used by deterministic triggers and by the model.  No decision is made here.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import statistics

from agent.config import ET
from agent.quant import structures as st
from agent.types import CONTRACT_MULTIPLIER, Leg


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _leg(raw: dict) -> Leg:
    return Leg(symbol=str(raw["symbol"]), ratio_qty=int(raw.get("ratio_qty", 1)),
               side=str(raw["side"]), position_intent=str(raw["position_intent"]),
               strike=float(raw["strike"]), option_type=str(raw["option_type"]),
               expiry=dt.date.fromisoformat(str(raw["expiry"])))


def _deadline(thesis) -> str:
    return str(getattr(thesis, "exit_at", "") or
               getattr(thesis, "exit_time", "") or "") if thesis else ""


def _minutes_to(deadline: str, now: dt.datetime) -> int | None:
    if not deadline:
        return None
    raw = deadline[:-3].strip() if deadline.upper().endswith(" ET") else deadline
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return max(int((parsed.astimezone(ET) - now.astimezone(ET)).total_seconds() / 60), 0)


def _exit_quote_metrics(legs: list[dict], quotes: dict[str, dict], qty: int) -> tuple:
    """Executable value and explicitly labelled current close-quote quality."""
    value, midpoint_value, full_width, missing = 0.0, 0.0, 0.0, []
    widest_spread_pct = 0.0
    for leg in legs:
        quote = quotes.get(str(leg.get("symbol"))) or {}
        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        if bid <= 0 or ask <= 0:
            missing.append(str(leg.get("symbol")))
            continue
        ratio = int(leg.get("ratio_qty", 1))
        midpoint = (bid + ask) / 2
        sign = 1 if leg.get("side") == "buy" else -1
        value += ratio * (bid if sign > 0 else -ask)
        midpoint_value += ratio * sign * midpoint
        full_width += ratio * (ask - bid)
        if midpoint > 0:
            widest_spread_pct = max(
                widest_spread_pct, (ask - bid) / midpoint * 100)
    valid = not missing
    liquidation = round(value, 4) if valid else None
    quality = {
        "all_exit_leg_quotes_valid": valid,
        "missing_exit_leg_symbols": missing,
        # One-way cost of immediately crossing every closing leg rather than
        # valuing the structure at quote midpoints, for the complete position.
        "close_crossing_cost_from_midpoint_dollars": (
            round(max(midpoint_value - value, 0.0) * qty * CONTRACT_MULTIPLIER, 2)
            if valid else None),
        # Sum of each leg's complete bid/ask width at the held ratios and quantity.
        # This is twice the midpoint-to-executable cost when quotes are symmetric.
        "aggregate_leg_bid_ask_width_dollars": (
            round(full_width * qty * CONTRACT_MULTIPLIER, 2) if valid else None),
        "widest_leg_bid_ask_spread_pct_of_mid": (
            round(widest_spread_pct, 4) if valid else None),
    }
    return liquidation, missing, quality


def _profit_target(structure: dict, thesis, params) -> tuple[float, dict]:
    """Resolve a typed target from actual reconciled entry economics."""
    basis = abs(_f(structure.get("cost_basis")))
    qty = max(abs(int(_f(structure.get("qty"), 0))), 1)
    signed_entry_price = _f(structure.get("cost_basis")) / (
        qty * CONTRACT_MULTIPLIER)
    policy = copy.deepcopy(getattr(thesis, "enforced_exit_policy", {}) or {})
    target = dict(policy.get("profit_target") or {})
    kind = str(target.get("kind") or "")
    value = _f(target.get("value"), 0.0)

    # Schema-1 policies applied entry-basis semantics to every premium type.  A
    # finite-profit debit structure is upgraded in place to the semantics its
    # thesis described; credit structures retain their natural credit fraction.
    if kind == "entry_basis_profit_pct":
        fraction = _f(target.get("value_pct"), params.profit_target_pct) / 100.0
        try:
            legs = [_leg(raw) for raw in structure.get("legs") or []]
            maximum = st.max_profit(legs, signed_entry_price, qty)
        except (KeyError, TypeError, ValueError):
            maximum = st.UNBOUNDED
        if signed_entry_price > 0 and maximum != st.UNBOUNDED:
            target = {"kind": "maximum_profit_fraction", "value": fraction}
        elif signed_entry_price < 0:
            target = {"kind": "entry_credit_fraction", "value": fraction}
        else:
            target = {"kind": "entry_basis_profit_fraction", "value": fraction}
        kind, value = target["kind"], float(target["value"])

    if kind == "profit_dollars":
        dollars = value
    elif kind == "entry_credit_fraction":
        dollars = basis * value
    elif kind == "entry_basis_profit_fraction":
        dollars = basis * value
    elif kind == "maximum_profit_fraction":
        try:
            legs = [_leg(raw) for raw in structure.get("legs") or []]
            maximum = st.max_profit(legs, signed_entry_price, qty)
        except (KeyError, TypeError, ValueError):
            maximum = st.UNBOUNDED
        dollars = maximum * value if maximum != st.UNBOUNDED else 0.0
    else:
        target = {"kind": "entry_basis_profit_fraction",
                  "value": params.profit_target_pct / 100.0}
        dollars = basis * float(target["value"])
    canonical = {
        **policy,
        "schema_version": max(int(policy.get("schema_version") or 0), 2),
        "profit_target": target,
        "resolved_profit_target_dollars": round(max(float(dollars), 0.0), 2),
        "resolved_from_filled_entry": True,
    }
    return canonical["resolved_profit_target_dollars"], canonical


def structure_view(structure: dict, thesis, quotes: dict[str, dict],
                   spots: dict[str, float], now: dt.datetime, params) -> dict:
    row = copy.deepcopy(structure)
    qty = max(abs(int(_f(row.get("qty"), 0))), 1)
    basis = _f(row.get("cost_basis"))
    unrealized = _f(row.get("unrealized_pl"))
    max_loss = _f(row.get("premium_at_risk"))
    signed_entry_price = basis / (qty * CONTRACT_MULTIPLIER)
    liquidation, missing, quote_quality = _exit_quote_metrics(
        row.get("legs") or [], quotes, qty)
    executable_pnl = ((liquidation - signed_entry_price)
                      * qty * CONTRACT_MULTIPLIER if liquidation is not None else None)
    is_long_premium = basis > 0

    credit_stop = (abs(basis) * max(params.short_premium_stop_multiple - 1.0, 0.0)
                   if not is_long_premium else 0.0)
    max_loss_stop = (max_loss * 0.50 if not is_long_premium
                     and math.isfinite(max_loss) else 0.0)
    stop_values = [x for x in (credit_stop, max_loss_stop) if x > 0]
    loss_stop = min(stop_values) if stop_values else None
    stop_progress = (max(-unrealized, 0.0) / loss_stop
                     if loss_stop and loss_stop > 0 else None)

    legs = []
    raw_spot = spots.get(row.get("underlying"))
    try:
        legs = [_leg(raw) for raw in row.get("legs") or []]
        breakevens = st.breakevens(legs, signed_entry_price)
        pnl_if_expired_now = (
            st.net_payoff_at(legs, float(raw_spot))
            - signed_entry_price * CONTRACT_MULTIPLIER
            if raw_spot is not None else None)
    except (KeyError, TypeError, ValueError):
        breakevens, pnl_if_expired_now = [], None
    spot = _f(raw_spot, 0.0)
    nearest = min(breakevens, key=lambda price: abs(price - spot)) if breakevens and spot else None
    deadline = _deadline(thesis)
    profit_target, profit_policy = _profit_target(row, thesis, params)

    row.update({
        "entry_price_per_unit": round(signed_entry_price, 4),
        "current_exit_value_per_unit": liquidation,
        "current_close_price_per_unit": (
            round(-liquidation, 4) if liquidation is not None and not is_long_premium
            else None),
        "executable_unrealized_pl": (round(executable_pnl, 2)
                                      if executable_pnl is not None else None),
        "broker_unrealized_pl": round(unrealized, 2),
        "pnl_pct_of_max_loss": (round(unrealized / max_loss, 4)
                                 if max_loss > 0 and math.isfinite(max_loss) else None),
        "premium_type": "long" if is_long_premium else "short",
        "loss_stop": round(loss_stop, 2) if loss_stop is not None else None,
        "loss_to_stop": (round(max(loss_stop + unrealized, 0.0), 2)
                          if loss_stop is not None else None),
        "stop_progress": round(stop_progress, 4) if stop_progress is not None else None,
        "profit_target": profit_target,
        "profit_target_policy": profit_policy,
        "breakevens": [round(price, 4) for price in breakevens],
        "spot": round(spot, 4) if spot else None,
        "nearest_breakeven": ({"price": round(nearest, 4),
                                "points_from_spot": round(nearest - spot, 4)}
                               if nearest is not None else None),
        "pnl_if_expired_now_per_unit": (round(pnl_if_expired_now, 2)
                                         if pnl_if_expired_now is not None else None),
        "exit_at": deadline,
        "minutes_to_exit": _minutes_to(deadline, now),
        "missing_exit_quotes": missing,
        "exit_quote_quality": quote_quality,
    })
    return row


def snapshot(account: dict, risk_state: dict, theses, quotes: dict[str, dict],
             spots: dict[str, float], now: dt.datetime, params) -> dict:
    structures = [structure_view(
        structure, theses.get(str(structure.get("thesis_id") or "")),
        quotes, spots, now, params) for structure in risk_state.get("structures") or []]
    return {
        "observed_at": now.astimezone(dt.timezone.utc).isoformat(),
        "equity": _f(account.get("equity")),
        "starting_equity": None,
        "total_unrealized_pl": round(sum(
            _f(row.get("broker_unrealized_pl")) for row in structures), 2),
        "total_executable_unrealized_pl": round(sum(
            _f(row.get("executable_unrealized_pl")) for row in structures
            if row.get("executable_unrealized_pl") is not None), 2),
        "premium_at_risk": _f(risk_state.get("premium_at_risk")),
        "realised_loss": _f(risk_state.get("realised_loss")),
        "structure_count": len(structures),
        "structures": structures,
    }


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[index]


def _executable_pnl_variation(points: list[dict], structure_id: str) -> dict:
    """Describe recent changes without labelling market movement as quote noise."""
    valid_values: list[float] = []
    valid_times: list[dt.datetime] = []
    successive_changes: list[float] = []
    previous: float | None = None
    for point in points:
        match = next((row for row in point.get("structures") or []
                      if row.get("structure_id") == structure_id), None)
        raw = match.get("executable_unrealized_pl") if match else None
        if raw is None:
            previous = None  # do not bridge a missing-quote interval
            continue
        value = float(raw)
        valid_values.append(value)
        try:
            valid_times.append(dt.datetime.fromisoformat(str(point.get("observed_at"))))
        except (TypeError, ValueError):
            pass
        if previous is not None:
            successive_changes.append(abs(value - previous))
        previous = value
    lookback = None
    if len(valid_times) >= 2:
        lookback = max(int((valid_times[-1] - valid_times[0]).total_seconds()), 0)
    median = statistics.median(successive_changes) if successive_changes else None
    p90 = _nearest_rank(successive_changes, 0.90)
    maximum = max(successive_changes) if successive_changes else None
    return {
        "lookback_seconds": lookback,
        "valid_executable_pnl_observation_count": len(valid_values),
        "successive_change_count": len(successive_changes),
        "median_absolute_successive_change_dollars": (
            round(median, 2) if median is not None else None),
        "p90_absolute_successive_change_dollars": (
            round(p90, 2) if p90 is not None else None),
        "maximum_absolute_successive_change_dollars": (
            round(maximum, 2) if maximum is not None else None),
    }


def with_trajectories(current: dict, history, max_points: int = 12,
                      variation_points: int = 60) -> dict:
    """Attach a bounded visible trajectory and compact longer-window statistics."""
    out = copy.deepcopy(current)
    history_rows = list(history)
    points = history_rows[-max_points:]
    variation_history = history_rows[-variation_points:]
    out["equity_trajectory"] = [
        {"at": row.get("observed_at"), "equity": row.get("equity"),
         "unrealized_pl": row.get("total_unrealized_pl")}
        for row in points]
    for structure in out.get("structures") or []:
        sid = structure.get("structure_id")
        trajectory = []
        for point in points:
            match = next((row for row in point.get("structures") or []
                          if row.get("structure_id") == sid), None)
            if match:
                trajectory.append({
                    "at": point.get("observed_at"),
                    "unrealized_pl": match.get("broker_unrealized_pl"),
                    "executable_unrealized_pl": match.get("executable_unrealized_pl"),
                    "stop_progress": match.get("stop_progress"),
                    "spot": match.get("spot"),
                })
        structure["pnl_trajectory"] = trajectory
        structure["recent_executable_pnl_variation"] = (
            _executable_pnl_variation(variation_history, str(sid)))
    return out
