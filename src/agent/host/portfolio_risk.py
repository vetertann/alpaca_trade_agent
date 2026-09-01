"""Correlated, executable portfolio stress and exact admission sizing.

The model may choose a structure, but this module owns the question that matters
at submission time: what happens to the *resulting book* under the same spot and
volatility shocks?  It is deliberately pure.  Callers provide resolved legs,
fresh quotes, spots, and the observation time; no broker or model access occurs
here.

P&L is measured from an executable baseline.  Existing structures start at their
current closeable value.  Candidate units start at the executable entry price.
The scenario value retains the observed half-spread on every leg, so a theoretical
midpoint move cannot masquerade as closeable profit.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from agent.config import ET
from agent.quant import bs
from agent.types import CONTRACT_MULTIPLIER, Leg

SCENARIO_SPOT_MOVES = (-1.0, -0.5, 0.0, 0.5, 1.0)
SCENARIO_IV_SHOCKS = (0.0, 0.20)
CORRELATED_CLUSTER = frozenset({"SPY", "QQQ", "IWM"})
EPSILON = 1e-9


@dataclass(frozen=True)
class PricedLeg:
    symbol: str
    underlying: str
    strike: float
    option_type: str
    expiry: dt.datetime
    sign: int
    ratio_qty: int
    iv: float
    half_spread: float
    executable_price: float


def _number(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _field(raw, name: str, default=None):
    return getattr(raw, name, raw.get(name, default) if isinstance(raw, Mapping) else default)


def _expiry(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime.combine(value, dt.time(16, 0))
    else:
        text = str(value)
        parsed = (dt.datetime.fromisoformat(text) if "T" in text
                  else dt.datetime.combine(dt.date.fromisoformat(text), dt.time(16, 0)))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed


def _intrinsic(spot: float, strike: float, option_type: str) -> float:
    return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)


def _option_value(leg: PricedLeg, spot: float, at: dt.datetime,
                  iv_multiplier: float) -> float:
    if at.astimezone(dt.timezone.utc) >= leg.expiry.astimezone(dt.timezone.utc):
        return _intrinsic(spot, leg.strike, leg.option_type)
    t = bs.year_fraction(at.astimezone(dt.timezone.utc),
                         leg.expiry.astimezone(dt.timezone.utc))
    return bs.price(spot, leg.strike, t, leg.iv * iv_multiplier, leg.option_type)


def _executable(mark: float, leg: PricedLeg) -> float:
    """Price received/paid when unwinding this opening-side leg."""
    if leg.sign > 0:  # sell a long at bid
        return max(mark - leg.half_spread, 0.0)
    return mark + leg.half_spread  # buy a short at ask


def price_legs(underlying: str, legs: Sequence[Leg | Mapping],
               quotes: Mapping[str, Mapping], spots: Mapping[str, float],
               as_of: dt.datetime,
               iv_by_symbol: Mapping[str, float] | None = None) -> tuple[list[PricedLeg], list[str]]:
    """Resolve fresh quotes into stress inputs and name every unusable symbol."""
    underlying = str(underlying).upper()
    spot = _number(spots.get(underlying))
    iv_by_symbol = iv_by_symbol or {}
    out: list[PricedLeg] = []
    missing: list[str] = []
    if spot <= 0:
        return [], [f"{underlying}:spot"]
    for raw in legs:
        symbol = str(_field(raw, "symbol", ""))
        quote = quotes.get(symbol) or {}
        bid, ask = _number(quote.get("bp"), -1), _number(quote.get("ap"), -1)
        if not symbol or bid < 0 or ask <= 0 or ask < bid:
            missing.append(symbol or "<missing-symbol>")
            continue
        strike = _number(_field(raw, "strike"))
        option_type = str(_field(raw, "option_type", "")).lower()
        if strike <= 0 or option_type not in ("call", "put"):
            missing.append(symbol)
            continue
        expiry = _expiry(_field(raw, "expiry"))
        midpoint = (bid + ask) / 2.0
        t = bs.year_fraction(as_of.astimezone(dt.timezone.utc),
                             expiry.astimezone(dt.timezone.utc))
        iv = _number(iv_by_symbol.get(symbol))
        if iv <= 0:
            iv = bs.implied_vol(midpoint, spot, strike, t, option_type) or 0.0
        if iv <= 0:
            missing.append(symbol)
            continue
        side = str(_field(raw, "side", ""))
        sign = 1 if side == "buy" else -1 if side == "sell" else 0
        ratio = int(_number(_field(raw, "ratio_qty", 1), 1))
        if sign == 0 or ratio < 1:
            missing.append(symbol)
            continue
        # Existing and candidate baselines both use the opening-side execution:
        # long positions can close at bid, shorts at ask; a new long enters at ask,
        # a new short at bid.  The caller chooses the relevant signed baseline.
        close_price = bid if sign > 0 else ask
        out.append(PricedLeg(
            symbol=symbol, underlying=underlying, strike=strike,
            option_type=option_type, expiry=expiry, sign=sign,
            ratio_qty=ratio, iv=iv, half_spread=(ask - bid) / 2.0,
            executable_price=close_price))
    return out, sorted(set(missing))


def signed_close_value(legs: Sequence[PricedLeg]) -> float:
    """Current executable liquidation value per structure unit."""
    return sum(leg.sign * leg.ratio_qty * leg.executable_price for leg in legs)


def signed_entry_value(legs: Sequence[PricedLeg], quotes: Mapping[str, Mapping]) -> float:
    """Current executable entry debit (positive) or credit (negative)."""
    total = 0.0
    for leg in legs:
        quote = quotes[leg.symbol]
        price = _number(quote.get("ap")) if leg.sign > 0 else _number(quote.get("bp"))
        total += leg.sign * leg.ratio_qty * price
    return total


def _scenario_value(legs: Sequence[PricedLeg], spots: Mapping[str, float],
                    at: dt.datetime, spot_multiple: float,
                    iv_shock: float, sigmas: Mapping[str, float],
                    horizon_days: float) -> tuple[float, dict[str, float]]:
    moved: dict[str, float] = {}
    value = 0.0
    for leg in legs:
        if leg.underlying not in moved:
            spot = _number(spots.get(leg.underlying))
            sigma = _number(sigmas.get(leg.underlying))
            move = spot * sigma * math.sqrt(max(horizon_days, 0.0) / 252.0)
            moved[leg.underlying] = max(spot + spot_multiple * move, 0.01)
        theoretical = _option_value(
            leg, moved[leg.underlying], at, 1.0 + iv_shock)
        value += leg.sign * leg.ratio_qty * _executable(theoretical, leg)
    return value, moved


def _aggregate_greeks(legs: Sequence[PricedLeg], qty: int,
                      spots: Mapping[str, float], as_of: dt.datetime) -> dict:
    delta = gamma = 0.0
    by_underlying: dict[str, dict[str, float]] = {}
    for leg in legs:
        spot = _number(spots.get(leg.underlying))
        t = bs.year_fraction(as_of.astimezone(dt.timezone.utc),
                             leg.expiry.astimezone(dt.timezone.utc))
        g = bs.greeks(spot, leg.strike, t, leg.iv, leg.option_type)
        units = leg.sign * leg.ratio_qty * qty
        delta += units * g.delta
        gamma += units * g.gamma
        row = by_underlying.setdefault(leg.underlying, {"net_delta": 0.0,
                                                         "net_gamma": 0.0})
        row["net_delta"] += units * g.delta
        row["net_gamma"] += units * g.gamma
    return {
        "net_delta": round(delta, 6), "net_gamma": round(gamma, 8),
        "by_underlying": {
            key: {"net_delta": round(value["net_delta"], 6),
                  "net_gamma": round(value["net_gamma"], 8)}
            for key, value in sorted(by_underlying.items())},
    }


def _median_iv(legs: Iterable[PricedLeg]) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for leg in legs:
        values.setdefault(leg.underlying, []).append(leg.iv)
    return {symbol: sorted(rows)[len(rows) // 2] for symbol, rows in values.items()}


def stress_portfolio(structures: Sequence[Mapping], quotes: Mapping[str, Mapping],
                     spots: Mapping[str, float], as_of: dt.datetime, *,
                     candidate: Mapping | None = None,
                     iv_by_symbol: Mapping[str, float] | None = None,
                     sigma_by_underlying: Mapping[str, float] | None = None,
                     horizon_days: float = 1.0,
                     spot_moves: Sequence[float] = SCENARIO_SPOT_MOVES,
                     iv_shocks: Sequence[float] = SCENARIO_IV_SHOCKS) -> dict:
    """Return current and candidate-unit P&L under one correlated scenario grid."""
    as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=dt.timezone.utc)
    priced_structures: list[tuple[str, int, list[PricedLeg], float]] = []
    missing: list[str] = []
    all_legs: list[PricedLeg] = []
    for index, structure in enumerate(structures):
        underlying = str(structure.get("underlying") or "").upper()
        priced, absent = price_legs(underlying, structure.get("legs") or [], quotes,
                                    spots, as_of, iv_by_symbol=iv_by_symbol)
        missing.extend(absent)
        qty = max(int(_number(structure.get("qty"), 0)), 0)
        baseline = signed_close_value(priced) if not absent else 0.0
        priced_structures.append((str(structure.get("structure_id") or index),
                                  qty, priced, baseline))
        all_legs.extend(priced)

    candidate_legs: list[PricedLeg] = []
    candidate_baseline = 0.0
    if candidate is not None:
        candidate_legs, absent = price_legs(
            str(candidate.get("underlying") or ""), candidate.get("legs") or [],
            quotes, spots, as_of, iv_by_symbol=iv_by_symbol)
        missing.extend(absent)
        if not absent:
            candidate_baseline = signed_entry_value(candidate_legs, quotes)
        all_legs.extend(candidate_legs)

    if missing:
        return {"status": "incomplete", "missing_symbols": sorted(set(missing)),
                "scenarios": [], "provenance": _provenance(as_of, horizon_days)}

    inferred_sigmas = _median_iv(all_legs)
    sigmas = dict(inferred_sigmas)
    for symbol, value in (sigma_by_underlying or {}).items():
        if _number(value) > 0:
            sigmas[str(symbol).upper()] = float(value)
    if any(_number(sigmas.get(symbol)) <= 0 for symbol in {l.underlying for l in all_legs}):
        absent = sorted(symbol for symbol in {l.underlying for l in all_legs}
                        if _number(sigmas.get(symbol)) <= 0)
        return {"status": "incomplete", "missing_symbols": [f"{s}:sigma" for s in absent],
                "scenarios": [], "provenance": _provenance(as_of, horizon_days)}

    scenario_at = as_of + dt.timedelta(days=horizon_days)
    scenarios: list[dict] = []
    for move in spot_moves:
        for shock in iv_shocks:
            current = 0.0
            by_structure: dict[str, float] = {}
            moved_spots: dict[str, float] = {}
            for sid, qty, legs, baseline in priced_structures:
                value, moved = _scenario_value(
                    legs, spots, scenario_at, float(move), float(shock), sigmas,
                    horizon_days)
                pnl = (value - baseline) * qty * CONTRACT_MULTIPLIER
                by_structure[sid] = round(pnl, 2)
                current += pnl
                moved_spots.update(moved)
            candidate_unit = 0.0
            if candidate_legs:
                value, moved = _scenario_value(
                    candidate_legs, spots, scenario_at, float(move), float(shock),
                    sigmas, horizon_days)
                candidate_unit = ((value - candidate_baseline)
                                  * CONTRACT_MULTIPLIER)
                moved_spots.update(moved)
            scenarios.append({
                "spot_expected_move_multiple": float(move),
                "iv_relative_shock": float(shock),
                "scenario_at": scenario_at.astimezone(dt.timezone.utc).isoformat(),
                "spots": {key: round(value, 4) for key, value in sorted(moved_spots.items())},
                "current_book_pnl": round(current, 2),
                "candidate_unit_pnl": round(candidate_unit, 2),
                "by_structure": by_structure,
            })

    worst_current = min(scenarios, key=lambda row: row["current_book_pnl"])
    current_greeks = _aggregate_greeks(
        [leg for _, qty, legs, _ in priced_structures for leg in legs for _ in range(qty)],
        1, spots, as_of) if priced_structures else _aggregate_greeks([], 1, spots, as_of)
    candidate_greeks = _aggregate_greeks(candidate_legs, 1, spots, as_of)
    return {
        "status": "ok", "missing_symbols": [], "scenarios": scenarios,
        "worst_current": {
            key: worst_current[key] for key in (
                "spot_expected_move_multiple", "iv_relative_shock",
                "current_book_pnl", "spots")},
        "current_greeks": current_greeks,
        "candidate_unit_greeks": candidate_greeks,
        "sigma_by_underlying": {key: round(value, 6)
                                 for key, value in sorted(sigmas.items())},
        "provenance": _provenance(as_of, horizon_days),
    }


def _provenance(as_of: dt.datetime, horizon_days: float) -> dict:
    return {
        "as_of": as_of.astimezone(dt.timezone.utc).isoformat(),
        "baseline": "executable current close for book; executable entry for candidate",
        "pricing": "Black-Scholes per leg with current midpoint-implied volatility",
        "friction": "observed per-leg half-spread retained in scenario close value",
        "correlation": "SPY/QQQ/IWM use the same expected-move sign and magnitude multiple",
        "horizon_trading_days": horizon_days,
    }


def feasible_quantity_interval(scenarios: Sequence[Mapping], loss_limit: float,
                               max_qty: int) -> dict:
    """Solve every linear scenario exactly for integer candidate quantity.

    Each row supplies ``current_book_pnl`` (B) and ``candidate_unit_pnl`` (C),
    and admission requires ``B + q*C >= -loss_limit``.  A breached book can
    therefore impose a *lower* bound on a repairing candidate; zero is not
    assumed feasible.
    """
    loss_limit = float(loss_limit)
    max_qty = max(int(max_qty), 0)
    lower, upper = 0, max_qty
    lower_binding = upper_binding = None
    constraints: list[dict] = []
    for index, row in enumerate(scenarios):
        base = float(row["current_book_pnl"])
        contribution = float(row["candidate_unit_pnl"])
        label = {"spot_expected_move_multiple": row.get("spot_expected_move_multiple"),
                 "iv_relative_shock": row.get("iv_relative_shock")}
        bound = None
        if contribution < -EPSILON:
            raw = (loss_limit + base) / (-contribution)
            bound = math.floor(raw + EPSILON)
            if bound < upper:
                upper, upper_binding = bound, label
            relation = "upper"
        elif contribution > EPSILON and base < -loss_limit - EPSILON:
            raw = (-loss_limit - base) / contribution
            bound = math.ceil(raw - EPSILON)
            if bound > lower:
                lower, lower_binding = bound, label
            relation = "lower"
        elif abs(contribution) <= EPSILON and base < -loss_limit - EPSILON:
            upper, upper_binding = -1, label
            relation = "infeasible"
        else:
            relation = "none"
        constraints.append({**label, "current_book_pnl": round(base, 2),
                            "candidate_unit_pnl": round(contribution, 2),
                            "relation": relation, "integer_bound": bound,
                            "scenario_index": index})
    feasible = lower <= upper and upper >= 0 and lower <= max_qty
    if not feasible:
        allowed = 0
    else:
        lower, upper = max(lower, 0), min(upper, max_qty)
        feasible = lower <= upper
        allowed = upper if feasible else 0
    return {
        "feasible": feasible,
        "minimum_qty": lower if feasible else None,
        "maximum_qty": upper if feasible else None,
        "allowed_qty": allowed,
        "ordinary_max_qty": max_qty,
        "lower_binding_scenario": lower_binding,
        "upper_binding_scenario": upper_binding,
        "constraints": constraints,
    }


def assess_admission(stress: Mapping, equity: float, loss_limit_pct: float,
                     ordinary_max_qty: int) -> dict:
    """Attach the calibrated dollar limit and exact quantity interval."""
    limit = max(float(equity), 0.0) * max(float(loss_limit_pct), 0.0) / 100.0
    if stress.get("status") != "ok":
        return {"status": "incomplete", "loss_limit_dollars": round(limit, 2),
                "loss_limit_pct": loss_limit_pct, "allowed_qty": 0,
                "missing_symbols": list(stress.get("missing_symbols") or [])}
    interval = feasible_quantity_interval(
        stress.get("scenarios") or [], limit, ordinary_max_qty)
    worst_current = min(float(row["current_book_pnl"])
                        for row in stress.get("scenarios") or [{"current_book_pnl": 0}])
    allowed = int(interval["allowed_qty"])
    resulting = [float(row["current_book_pnl"]) + allowed
                 * float(row["candidate_unit_pnl"])
                 for row in stress.get("scenarios") or []]
    return {
        "status": "ok", "loss_limit_dollars": round(limit, 2),
        "loss_limit_pct": float(loss_limit_pct),
        "current_worst_pnl": round(worst_current, 2),
        "current_breached": worst_current < -limit - EPSILON,
        "allowed_qty": allowed,
        "resulting_worst_pnl": round(min(resulting), 2) if resulting else 0.0,
        "resulting_breached": bool(resulting and min(resulting) < -limit - EPSILON),
        "interval": interval,
    }


def evidence_risk_ceiling(evidence: Mapping | None, equity: float, *,
                          robust_pct: float, supported_pct: float,
                          partial_pct: float) -> dict:
    """Turn recorded ensemble evidence into a host-owned dollar ceiling.

    Robust admission requires three positive measures *and* membership in the
    cross-measure stable top set.  Three positive but unstable, or two positive
    and stable, get the supported ceiling.  Two positive but unstable get only
    the partial ceiling.  Anything weaker is a refusal, regardless of the risk
    budget written by generated code.
    """
    evidence = evidence or {}
    evaluation = evidence.get("evaluation") or {}
    ranking = evidence.get("ranking") or {}
    candidate = str(evaluation.get("candidate") or "")
    raw_edges = evaluation.get("edge_by_measure") or {}
    edges = {str(name): float(value) for name, value in raw_edges.items()
             if isinstance(value, (int, float)) and math.isfinite(float(value))}
    values = list(edges.values())
    positive = sum(value > 0 for value in values)
    median = statistics.median(values) if values else 0.0
    stable = bool(candidate and candidate in set(ranking.get("stable_top") or []))
    if len(values) >= 3 and positive == len(values) and stable:
        tier, pct = "robust", float(robust_pct)
    elif median > 0 and (
            (len(values) >= 3 and positive == len(values))
            or (positive >= 2 and stable)):
        tier, pct = "supported", float(supported_pct)
    elif positive >= 2 and median > 0:
        tier, pct = "partial", float(partial_pct)
    else:
        tier, pct = "insufficient", 0.0
    return {
        "tier": tier, "candidate": candidate, "measure_count": len(values),
        "positive_measure_count": positive, "edge_median": round(median, 8),
        "stable_top": stable, "edge_by_measure": edges,
        "ceiling_pct": pct,
        "ceiling_dollars": max(float(equity), 0.0) * pct / 100.0,
    }
