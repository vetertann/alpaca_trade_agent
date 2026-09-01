"""Durable reconstruction helpers for portfolio-risk calibration.

The live gate and the historical replay share :mod:`portfolio_risk`.  This module
only turns immutable execution artifacts into chronological events and validates
that broker parent and child fills agree before any inferred volatility is used.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from agent.config import ET
from agent.host import portfolio_risk
from agent.quant import bs


def _at(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON") from exc
    return rows


def load_events(directory: str | Path, sample: str) -> list[dict]:
    """Join PRE_SUBMIT descriptors to nested broker fills and validate them."""
    directory = Path(directory)
    execution = _read_jsonl(directory / "execution.jsonl")
    orders = json.loads((directory / "nested_orders.json").read_text())
    prepared = {str(row["client_order_id"]): row for row in execution
                if row.get("kind") == "PRE_SUBMIT"}
    events: list[dict] = []
    for order in orders:
        coid = str(order.get("client_order_id") or "")
        descriptor = prepared.get(coid)
        if descriptor is None or str(order.get("status")) != "filled":
            continue
        parent_qty = float(order.get("filled_qty") or 0)
        parent_price = float(order["filled_avg_price"])
        child_by_symbol = {str(row.get("symbol")): row
                           for row in order.get("legs") or []}
        child_fills = []
        signed_sum = 0.0
        for leg in descriptor.get("legs") or []:
            symbol = str(leg["symbol"])
            child = child_by_symbol.get(symbol)
            if child is None or child.get("filled_avg_price") is None:
                raise ValueError(f"{sample} {coid}: missing child fill for {symbol}")
            expected_qty = parent_qty * int(leg.get("ratio_qty", 1))
            child_qty = float(child.get("filled_qty") or 0)
            if not math.isclose(child_qty, expected_qty, abs_tol=1e-9):
                raise ValueError(
                    f"{sample} {coid}: {symbol} child qty {child_qty} != {expected_qty}")
            fill_price = float(child["filled_avg_price"])
            sign = 1 if str(leg.get("side")) == "buy" else -1
            signed_sum += sign * int(leg.get("ratio_qty", 1)) * fill_price
            child_fills.append({**leg, "filled_qty": child_qty,
                                "filled_avg_price": fill_price,
                                "filled_at": child.get("filled_at")})
        if not math.isclose(signed_sum, parent_price, abs_tol=0.015):
            raise ValueError(
                f"{sample} {coid}: child net {signed_sum:.4f} != parent {parent_price:.4f}")
        events.append({
            "sample": sample, "client_order_id": coid,
            "order_id": str(order.get("id") or ""),
            "purpose": str(descriptor.get("purpose") or "entry"),
            "structure_id": str(descriptor["structure_id"]),
            "underlying": str(descriptor["underlying"]).upper(),
            "family": str(descriptor["family"]),
            "qty": int(parent_qty), "parent_filled_avg_price": parent_price,
            "parent_child_price_difference": round(parent_price - signed_sum, 6),
            "submitted_at": str(order["submitted_at"]),
            "filled_at": str(order.get("filled_at") or order["submitted_at"]),
            "legs": child_fills,
        })
    return sorted(events, key=lambda row: _at(row["filled_at"]))


def load_series(directory: str | Path) -> dict:
    raw = json.loads((Path(directory) / "series.json").read_text())
    return raw.get("minute") or {}


def nearest_series_price(series: Mapping, symbol: str,
                         at: dt.datetime) -> tuple[float, str, float]:
    rows = series.get(symbol) or []
    if not rows:
        raise ValueError(f"no captured series for {symbol}")
    parsed = [(_at(row[0]), float(row[1])) for row in rows]
    times = [row[0] for row in parsed]
    index = bisect.bisect_left(times, at)
    candidates = parsed[max(index - 1, 0):min(index + 1, len(parsed))]
    chosen_at, price = min(candidates, key=lambda row: abs((row[0] - at).total_seconds()))
    return price, chosen_at.isoformat(), abs((chosen_at - at).total_seconds())


def nearest_trade(rows: Sequence[Mapping], at: dt.datetime, *,
                  before: bool = False) -> dict | None:
    candidates = []
    for row in rows:
        try:
            row_at = _at(str(row["t"]))
            price = float(row["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if before and row_at > at:
            continue
        candidates.append((abs((row_at - at).total_seconds()), row_at, price, row))
    if not candidates:
        return None
    age, row_at, price, raw = min(candidates, key=lambda item: item[0])
    return {"price": price, "at": row_at.isoformat(), "age_seconds": round(age, 6),
            "raw": dict(raw)}


def _expiry(leg: Mapping) -> dt.datetime:
    return dt.datetime.combine(
        dt.date.fromisoformat(str(leg["expiry"])), dt.time(16), tzinfo=ET)


def leg_iv(price: float, spot: float, leg: Mapping,
           at: dt.datetime) -> float | None:
    return bs.implied_vol(
        float(price), float(spot), float(leg["strike"]),
        bs.year_fraction(at.astimezone(dt.timezone.utc),
                         _expiry(leg).astimezone(dt.timezone.utc)),
        str(leg["option_type"]))


def _model_price(spot: float, leg: Mapping, iv: float,
                 at: dt.datetime) -> float:
    expiry = _expiry(leg)
    if at.astimezone(dt.timezone.utc) >= expiry.astimezone(dt.timezone.utc):
        strike = float(leg["strike"])
        return max(spot - strike, 0.0) if leg["option_type"] == "call" \
            else max(strike - spot, 0.0)
    return bs.price(
        spot, float(leg["strike"]),
        bs.year_fraction(at.astimezone(dt.timezone.utc),
                         expiry.astimezone(dt.timezone.utc)),
        iv, str(leg["option_type"]))


def _event_market(market: Mapping | None, coid: str) -> Mapping:
    return ((market or {}).get("events") or {}).get(coid) or {}


def replay_sample(directory: str | Path, sample: str, *,
                  thresholds: Sequence[float], horizon_days: float = 1.0) -> dict:
    """Replay one account independently; never merge its book with another."""
    directory = Path(directory)
    events = load_events(directory, sample)
    series = load_series(directory)
    runtime = json.loads((directory / "runtime_state.json").read_text())
    equity = float(runtime.get("starting_equity") or 100_000.0)
    market_path = directory / "market_trades.json"
    market = json.loads(market_path.read_text()) if market_path.exists() else None
    open_structures: list[dict] = []
    policy_books: dict[float, list[dict]] = {
        round(float(threshold), 4): [] for threshold in thresholds}
    iv_by_symbol: dict[str, float] = {}
    rows = []
    for event in events:
        event_at = _at(event["filled_at"])
        if event["purpose"] == "exit":
            open_structures = [row for row in open_structures
                               if row["structure_id"] != event["structure_id"]]
            for threshold in policy_books:
                policy_books[threshold] = [
                    row for row in policy_books[threshold]
                    if row["structure_id"] != event["structure_id"]]
            continue
        relevant = {event["underlying"]} | {
            str(row["underlying"]) for row in open_structures}
        spots: dict[str, float] = {}
        spot_provenance: dict[str, dict] = {}
        market_event = _event_market(market, event["client_order_id"])
        for symbol in sorted(relevant):
            trade = nearest_trade(
                ((market_event.get("stock_trades") or {}).get(symbol) or []),
                event_at)
            if trade:
                spots[symbol] = float(trade["price"])
                spot_provenance[symbol] = {"source": "historical_trade", **trade}
            else:
                price, observed, age = nearest_series_price(series, symbol, event_at)
                spots[symbol] = price
                spot_provenance[symbol] = {
                    "source": "captured_minute_midpoint", "at": observed,
                    "age_seconds": round(age, 3), "price": price}

        fill_ivs: dict[str, float] = {}
        sensitivity: dict[str, dict] = {}
        for leg in event["legs"]:
            symbol = str(leg["symbol"])
            iv = leg_iv(float(leg["filled_avg_price"]), spots[event["underlying"]],
                        leg, event_at)
            if iv is None:
                raise ValueError(f"{sample} {event['client_order_id']}: cannot invert {symbol}")
            fill_ivs[symbol] = iv
            prior = nearest_trade(
                ((market_event.get("option_trades") or {}).get(symbol) or []),
                _at(event["submitted_at"]), before=True)
            prior_iv = (leg_iv(float(prior["price"]), spots[event["underlying"]],
                               leg, event_at) if prior else None)
            sensitivity[symbol] = {
                "side": leg["side"], "fill_price": leg["filled_avg_price"],
                "fill_iv": round(iv, 8), "prior_trade": prior,
                "prior_trade_iv": round(prior_iv, 8) if prior_iv else None,
                "iv_difference": (round(iv - prior_iv, 8)
                                  if prior_iv is not None else None),
            }

        # Existing legs are marked from a nearby pre-submission print when one is
        # available; otherwise their last fill-derived IV is propagated through
        # Black-Scholes. Historical bid/ask remains unavailable in both branches.
        quotes: dict[str, dict] = {}
        current_ivs = dict(iv_by_symbol)
        mark_provenance: dict[str, dict] = {}
        for structure in open_structures:
            underlying = str(structure["underlying"])
            for leg in structure["legs"]:
                symbol = str(leg["symbol"])
                prior = nearest_trade(
                    ((market_event.get("option_trades") or {}).get(symbol) or []),
                    _at(event["submitted_at"]), before=True)
                prior_iv = (leg_iv(float(prior["price"]), spots[underlying], leg,
                                   event_at) if prior else None)
                if prior and prior_iv and float(prior["age_seconds"]) <= 600:
                    price, current_ivs[symbol] = float(prior["price"]), prior_iv
                    mark_provenance[symbol] = {
                        "source": "nearest_prior_option_trade", **prior}
                else:
                    iv = current_ivs[symbol]
                    price = _model_price(spots[underlying], leg, iv, event_at)
                    mark_provenance[symbol] = {
                        "source": "fill_iv_model", "iv": round(iv, 8)}
                quotes[symbol] = {"bp": price, "ap": price}
        for leg in event["legs"]:
            price = float(leg["filled_avg_price"])
            quotes[str(leg["symbol"])] = {"bp": price, "ap": price}
            current_ivs[str(leg["symbol"])] = fill_ivs[str(leg["symbol"])]

        candidate = {"underlying": event["underlying"], "legs": event["legs"]}
        stress = portfolio_risk.stress_portfolio(
            open_structures, quotes, spots, event_at, candidate=candidate,
            iv_by_symbol=current_ivs, horizon_days=horizon_days)
        if stress.get("status") != "ok":
            raise ValueError(
                f"{sample} {event['client_order_id']}: incomplete stress {stress}")
        actual_qty = int(event["qty"])
        actual_scenarios = [
            {**scenario, "actual_resulting_pnl": round(
                float(scenario["current_book_pnl"])
                + actual_qty * float(scenario["candidate_unit_pnl"]), 2)}
            for scenario in stress["scenarios"]]
        actual_worst = min(row["actual_resulting_pnl"] for row in actual_scenarios)
        grid = []
        for threshold in thresholds:
            threshold_key = round(float(threshold), 4)
            policy_stress = portfolio_risk.stress_portfolio(
                policy_books[threshold_key], quotes, spots, event_at,
                candidate=candidate, iv_by_symbol=current_ivs,
                horizon_days=horizon_days)
            admission = portfolio_risk.assess_admission(
                policy_stress, equity, threshold, actual_qty)
            allowed_qty = int(admission["allowed_qty"])
            grid.append({
                "threshold_pct": threshold_key,
                "allowed_qty": allowed_qty,
                "actual_qty": actual_qty,
                "decision": ("pass" if allowed_qty == actual_qty
                             else "reduce" if allowed_qty > 0
                             else "refuse"),
                "policy_book_structure_count_before": len(
                    policy_books[threshold_key]),
                "policy_current_worst_pnl": admission.get("current_worst_pnl"),
                "resulting_worst_pnl": admission.get("resulting_worst_pnl"),
            })
            if allowed_qty > 0:
                policy_books[threshold_key].append({
                    "structure_id": event["structure_id"],
                    "underlying": event["underlying"], "family": event["family"],
                    "qty": allowed_qty, "legs": event["legs"]})
        rows.append({
            "client_order_id": event["client_order_id"],
            "structure_id": event["structure_id"], "at": event["filled_at"],
            "underlying": event["underlying"], "family": event["family"],
            "actual_qty": actual_qty,
            "book_structure_count_before": len(open_structures),
            "parent_filled_avg_price": event["parent_filled_avg_price"],
            "parent_child_price_difference": event["parent_child_price_difference"],
            "spots": spots, "spot_provenance": spot_provenance,
            "per_leg_iv": sensitivity, "existing_mark_provenance": mark_provenance,
            "actual_worst_pnl": actual_worst,
            "required_loss_limit_pct": round(max(-actual_worst, 0) / equity * 100, 6),
            "binding_actual_scenario": min(
                actual_scenarios, key=lambda row: row["actual_resulting_pnl"]),
            "current_worst_pnl": stress["worst_current"]["current_book_pnl"],
            "sigma_by_underlying": stress["sigma_by_underlying"],
            "threshold_grid": grid,
        })
        open_structures.append({
            "structure_id": event["structure_id"],
            "underlying": event["underlying"], "family": event["family"],
            "qty": actual_qty, "legs": event["legs"]})
        iv_by_symbol.update(fill_ivs)

    threshold_summary = []
    for threshold in thresholds:
        decisions = [next(cell for cell in row["threshold_grid"]
                          if cell["threshold_pct"] == round(float(threshold), 4))
                     for row in rows]
        threshold_summary.append({
            "threshold_pct": round(float(threshold), 4),
            "passed_entries": sum(cell["decision"] == "pass" for cell in decisions),
            "reduced_entries": sum(cell["decision"] == "reduce" for cell in decisions),
            "refused_entries": sum(cell["decision"] == "refuse" for cell in decisions),
            "allowed_contracts": sum(int(cell["allowed_qty"]) for cell in decisions),
            "actual_contracts": sum(int(cell["actual_qty"]) for cell in decisions),
        })
    return {
        "sample": sample, "equity": equity, "entry_count": len(rows),
        "events": rows, "threshold_summary": threshold_summary,
        "provenance": {
            "geometry_quantity_sequence": "execution.jsonl PRE_SUBMIT + nested broker fills",
            "per_leg_prices": "nested broker child filled_avg_price",
            "spot": "historical stock trade when captured; saved minute midpoint fallback",
            "existing_option_marks": (
                "nearest prior trade within 600s, otherwise fill-IV Black-Scholes model"),
            "historical_bid_ask": "unavailable; replay friction is approximate",
        },
    }
