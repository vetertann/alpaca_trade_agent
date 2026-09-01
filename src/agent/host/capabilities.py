"""Capability dispatch.

Maps namespace calls arriving from the sandbox onto real implementations, with
the policy verifier in front of anything that touches the broker. This is the
only path from generated code to the outside world.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import re
from typing import Any

from agent.config import ET, WINDOW_CLOSE
from agent.host import telemetry
from agent.host.action_triggers import ActionTriggerStore, entry_condition
from agent.host.execution import Executor
from agent.host.exit_policy import ExitPolicyStore
from agent.host.rest import Rest
from agent.host.risk_params import RiskParams
from agent.host.series import RollingSeries
from agent.host.settlement import SettlementAuthorizationStore
from agent.host.thesis_store import ThesisStore
from agent.quant import bs, candidates as cand, measures as ms, score_horizon, vol
from agent.quant import structures as st
from agent.types import CONTRACT_MULTIPLIER, Leg, TradeIntent


class CapabilityError(RuntimeError):
    pass


def _leg_from_dict(d: dict) -> Leg:
    return Leg(symbol=d["symbol"], ratio_qty=int(d.get("ratio_qty", 1)),
               side=d["side"], position_intent=d["position_intent"],
               strike=float(d["strike"]), option_type=d["option_type"],
               expiry=dt.date.fromisoformat(str(d["expiry"])))


def _evenly_spaced(values: list[str], count: int) -> list[str]:
    """Deterministically retain coverage of a sorted catalogue."""
    if count >= len(values):
        return values
    if count <= 1:
        return [values[0]] if values else []
    indices = [round(i * (len(values) - 1) / (count - 1)) for i in range(count)]
    return [values[i] for i in dict.fromkeys(indices)]


def _diverse(cands: list, limit: int) -> list:
    """Round-robin across expiries and families.

    Raw economics or crossing-cost order can otherwise fill the entire returned
    sample with one tenor or unbounded-profit family.  Every listed expiry gets a
    chance before a second candidate from the same expiry; within each expiry,
    families rotate and the cheapest-to-cross member of each family comes first.
    When the caller deliberately asks for fewer rows than expiries, sample the
    full calendar evenly instead of silently returning only the nearest dates.
    """
    if not cands or limit <= 0:
        return []
    expiries = sorted({str(getattr(c, "expiry", "")) for c in cands})
    selected = _evenly_spaced(expiries, min(limit, len(expiries)))
    queues: dict[str, list] = {}
    for expiry_index, expiry in enumerate(selected):
        by_family: dict[str, list] = {}
        rows = [c for c in cands if str(getattr(c, "expiry", "")) == expiry]
        for c in sorted(rows, key=lambda item: item.spread_cost_pct):
            by_family.setdefault(c.family, []).append(c)
        families = sorted(by_family)
        queue: list = []
        layer = 0
        while any(len(rows) > layer for rows in by_family.values()):
            if families:
                offset = (expiry_index + layer) % len(families)
                rotated = families[offset:] + families[:offset]
            else:
                rotated = []
            for family in rotated:
                if len(by_family[family]) > layer:
                    queue.append(by_family[family][layer])
            layer += 1
        queues[expiry] = queue

    out: list = []
    row = 0
    while len(out) < limit and any(len(queue) > row for queue in queues.values()):
        for expiry in selected:
            queue = queues[expiry]
            if len(queue) > row and len(out) < limit:
                out.append(queue[row])
        row += 1
    return out


def _counts_by_expiry(cands: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for candidate in cands:
        expiry = str(getattr(candidate, "expiry", ""))
        out[expiry] = out.get(expiry, 0) + 1
    return dict(sorted(out.items()))


def _intent_from_dict(d: dict) -> TradeIntent:
    return TradeIntent(underlying=d["underlying"], family=d.get("family", "custom"),
                       legs=tuple(_leg_from_dict(x) for x in d["legs"]),
                       thesis_id=d["thesis_id"],
                       risk_budget=float(d.get("risk_budget", 0.0)),
                       note=d.get("note", ""))


class Capabilities:
    """One instance per decision cycle."""

    def __init__(self, rest: Rest, series: RollingSeries, theses: ThesisStore,
                 executor: Executor, params: RiskParams, *, equity: float,
                 open_positions: list | None = None, realised_loss: float = 0.0,
                 open_premium_at_risk: float = 0.0,
                 trigger: dict | None = None,
                 exit_policies: ExitPolicyStore | None = None,
                 action_triggers: ActionTriggerStore | None = None,
                 settlement_authorizations: SettlementAuthorizationStore | None = None,
                 scheduled_events: dict | None = None,
                 current_scenario_breached: bool = False):
        self.rest, self.series, self.theses = rest, series, theses
        self.ex, self.params = executor, params
        self.equity = equity
        self.open_positions = open_positions or []
        self.realised_loss = realised_loss
        self.open_premium_at_risk = open_premium_at_risk
        self.trigger = dict(trigger or {})
        self.exit_policies = exit_policies
        self.action_triggers = action_triggers
        self.settlement_authorizations = settlement_authorizations
        self.scheduled_events = copy.deepcopy(scheduled_events or {})
        self.current_scenario_breached = bool(current_scenario_breached)
        self._contracts: dict[str, dict] = {}
        self._measures: dict[str, list] = {}      # handles; samples never cross the pipe
        self._candidates: dict[str, cand.Candidate] = {}
        # Successful, cycle-local evidence.  A tool name alone is not proof: every
        # record is bound to the exact candidate geometry and distribution inputs.
        self._enumerated: set[tuple] = set()
        self._measure_context: dict[str, dict] = {}
        self._evaluated: list[dict] = []
        self._ranked: list[dict] = []
        self._direction_checked: list[dict] = []
        self._directional_context_checked: list[dict] = []
        self._news_reviewed = False
        self._news_rows: list[dict] = []
        self._news_queries: list[set[str] | None] = []
        self._program_decision: dict[str, str] | None = None
        self._submitted_this_program = False
        self._trading_result: dict | None = None

    def begin_program(self) -> None:
        """Reset the explicit control result at each model-program boundary."""
        self._program_decision = None
        self._submitted_this_program = False
        self._trading_result = None

    @property
    def program_decision(self) -> dict[str, str] | None:
        return dict(self._program_decision) if self._program_decision else None

    @property
    def trading_result(self) -> dict | None:
        return dict(self._trading_result) if self._trading_result else None

    # ---- dispatch ----------------------------------------------------------
    def dispatch(self, ns: str, fn: str, args: list, kwargs: dict) -> Any:
        handler = getattr(self, f"_{ns}_{fn}", None)
        if handler is None:
            raise AttributeError(
                f"{ns}.{fn} does not exist. See the capability list in the preamble.")
        tool = f"{ns}.{fn}"
        with telemetry.execute_tool(tool, telemetry.new_call_id(),
                                    arguments={"args": args, "kwargs": kwargs}) as span:
            try:
                return handler(*args, **kwargs)
            except Exception as exc:
                telemetry.record_error(span, exc)
                raise

    # ---- decision control --------------------------------------------------
    def _set_program_decision(self, status: str, reason: str) -> dict[str, str]:
        reason = str(reason).strip()
        if not reason:
            raise CapabilityError("decision reason must be non-empty")
        if self._submitted_this_program:
            raise CapabilityError("an order was already submitted in this program")
        if self._program_decision is not None:
            raise CapabilityError(
                f"program already ended with {self._program_decision['status']!r}")
        self._program_decision = {"status": status, "reason": reason}
        return dict(self._program_decision)

    def _decision_no_trade(self, reason):
        """End safely without submission, discarding any unsubmitted draft."""
        if self._submitted_this_program:
            raise CapabilityError("cannot declare no-trade after submitting an order")
        discarded = self.ex.latest_staged is not None
        out = self._set_program_decision("no_trade", reason)
        if discarded:
            self.ex.discard_staged()
        return {**out, "discarded_staged": discarded}

    # ---- market ------------------------------------------------------------
    def _market_latest_quote(self, symbols):
        syms = [symbols] if isinstance(symbols, str) else list(symbols)
        return {s: {"mid": self.series.last(s)} for s in syms}

    def _market_bars(self, symbol, timeframe="1Day", start=None, end=None):
        """Basic cannot pull the most recent 15 minutes, so `end` defaults behind it."""
        if end is None:
            end = (dt.datetime.now(dt.timezone.utc)
                   - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
        start = start or (dt.date.today() - dt.timedelta(days=120)).isoformat()
        return self.rest.stock_bars(symbol, timeframe, start, end)

    def _market_spot(self, symbol):
        live = self.series.last(symbol)
        return live if live else float(self.rest.stock_latest_trade(symbol)["p"])

    def _market_session_range(self, symbol):
        r = self.series.session_range(symbol)
        return {"low": r[0], "high": r[1]} if r else None

    def _market_directional_context(self, symbol):
        """Observed multi-horizon price direction, never an inferred forecast."""
        symbol = str(symbol).upper()
        contexts = self.series.directional_contexts(
            {"SPY", "QQQ", "IWM", symbol}, dt.datetime.now(dt.timezone.utc))
        result = copy.deepcopy(contexts[symbol])
        self._directional_context_checked.append({"symbol": symbol,
                                                  "result": copy.deepcopy(result)})
        return result

    def _market_news(self, symbols=None, limit=20):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        rows = self.rest.news(symbols, start, limit)
        self._news_reviewed = True
        self._news_rows = list(rows[:10])
        if symbols is None:
            self._news_queries.append(None)  # unfiltered review covers every candidate
        else:
            requested = [symbols] if isinstance(symbols, str) else list(symbols)
            self._news_queries.append({str(symbol).upper() for symbol in requested})
        return rows

    # ---- options -----------------------------------------------------------
    def _options_contracts(self, underlying, exp_gte, exp_lte):
        rows = self.rest.contracts(underlying, exp_gte, exp_lte)
        for c in rows:
            self._contracts[c["symbol"]] = c
        return rows

    def _options_expiries(self, underlying):
        """Every active broker-listed expiry; no competition-calendar cutoff."""
        today = dt.datetime.now(ET).date().isoformat()
        rows = self.rest.contracts(str(underlying).upper(), today, None)
        expiries = sorted({str(row["expiration_date"]) for row in rows})
        return {
            "underlying": str(underlying).upper(),
            "as_of_et": dt.datetime.now(ET).isoformat(timespec="seconds"),
            "count": len(expiries),
            "expiries": expiries,
            "eligibility": (
                "all active broker-listed expiries; score at earlier of expiry "
                "and official equity mark"),
        }

    def _options_chain(self, underlying, exp_gte, exp_lte, around=None, width=10):
        """Contracts joined to live quotes, restricted to a strike band."""
        spot = around or self._market_spot(underlying)
        now = dt.datetime.now(dt.timezone.utc)
        rows = [c for c in self._options_contracts(underlying, exp_gte, exp_lte)
                if abs(float(c["strike_price"]) - spot) <= width]
        quotes = self.rest.option_quotes([c["symbol"] for c in rows])
        out = []
        for c in rows:
            q = quotes.get(c["symbol"])
            if not q:
                continue
            bid, ask = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
            if bid <= 0 or ask <= 0:
                continue                       # zero bid means no exit exists
            mid = (bid + ask) / 2
            expiry = dt.datetime.fromisoformat(c["expiration_date"]).replace(
                hour=16, tzinfo=ET)
            t = bs.year_fraction(now, expiry)
            iv = bs.implied_vol(mid, spot, float(c["strike_price"]), t, c["type"])
            delta = (bs.greeks(spot, float(c["strike_price"]), t, iv, c["type"]).delta
                     if iv is not None else None)
            out.append({"symbol": c["symbol"], "strike": float(c["strike_price"]),
                        "option_type": c["type"], "expiry": c["expiration_date"],
                        "bid": bid, "ask": ask, "mid": mid,
                        "spread_pct": (ask - bid) / mid * 100,
                        "iv": None if iv is None else round(iv, 6),
                        "delta": None if delta is None else round(delta, 6),
                        "open_interest": c.get("open_interest")})
        return sorted(out, key=lambda r: (r["expiry"], r["option_type"], r["strike"]))

    def _options_tradeable_chain(self, underlying, exp_gte, exp_lte, around=None,
                                 width=10, max_spread_pct=None):
        cap = max_spread_pct or self.params.max_spread_pct_of_mid
        return [r for r in self._options_chain(underlying, exp_gte, exp_lte, around, width)
                if r["spread_pct"] <= cap]

    def _options_greeks(self, symbol, spot=None, iv=None, now=None):
        c = self._contracts.get(symbol)
        if not c:
            raise CapabilityError(f"{symbol} not in the session contract cache; "
                                  "call options.contracts or options.chain first")
        spot = spot or self._market_spot(c["underlying_symbol"])
        expiry = dt.datetime.fromisoformat(c["expiration_date"]).replace(hour=16, tzinfo=ET)
        t = bs.year_fraction(now or dt.datetime.now(dt.timezone.utc), expiry)
        if iv is None:
            q = self.rest.option_quotes([symbol]).get(symbol, {})
            mid = (float(q.get("bp", 0)) + float(q.get("ap", 0))) / 2
            iv = bs.implied_vol(mid, spot, float(c["strike_price"]), t, c["type"])
            if iv is None:
                raise CapabilityError(f"{symbol}: quote is unusable, no implied vol")
        g = bs.greeks(spot, float(c["strike_price"]), t, iv, c["type"])
        return {"iv": iv, "delta": g.delta, "gamma": g.gamma, "theta": g.theta,
                "vega": g.vega, "rho": g.rho, "t_years": t}

    def _options_payoff(self, legs, net_price, qty=1, points=40):
        return st.payoff_curve([_leg_from_dict(l) for l in legs], net_price, qty, points)

    def _options_enumerate(self, underlying, exp_gte, exp_lte, families=None,
                           widths=(1, 2, 3, 5, 10), width=10, max_spread_pct=None,
                           min_risk_reward=0.50, max_loss_cap=None, limit=240):
        """Deterministic structure search over the liquidity-gated chain."""
        chain = self._options_tradeable_chain(underlying, exp_gte, exp_lte,
                                              width=width, max_spread_pct=max_spread_pct)
        if not chain:
            return {"spot": round(self._market_spot(underlying), 2),
                    "generated": 0, "kept": 0, "families": [],
                    "expiry_coverage": {"generated": {}, "kept": {},
                                        "returned": {}},
                    "note": "no tradeable contracts under the liquidity gate for "
                            "that range", "candidates": []}
        spot = self._market_spot(underlying)
        fams = tuple(families) if families else cand.FAMILIES
        found = cand.enumerate_structures(chain, spot, underlying=underlying,
                                          families=fams, widths=tuple(widths))
        kept = cand.filter_candidates(found, min_risk_reward=min_risk_reward,
                                      max_loss_cap=max_loss_cap)
        observed_at = dt.datetime.now(dt.timezone.utc)
        for c in kept:
            c.detail.update(score_horizon.candidate_horizon(c.expiry, observed_at))
            self._candidates[c.id] = c
            self._enumerated.add(self._candidate_signature(c))
        shown = _diverse(kept, int(limit))
        return {"spot": round(spot, 2), "generated": len(found), "kept": len(kept),
                "families": sorted({c.family for c in kept}),
                "expiry_coverage": {"generated": _counts_by_expiry(found),
                                    "kept": _counts_by_expiry(kept),
                                    "returned": _counts_by_expiry(shown)},
                "note": "sampled across expiry and family, cheapest crossing cost "
                        "first within each bucket; this result can be large, so "
                        "process it inside the program and print only a capped "
                        "summary; rank on edge with vol.evaluate",
                "candidates": [c.to_json() for c in shown]}

    # ---- vol ---------------------------------------------------------------
    def _vol_realized(self, symbol, lookback=60, window=20):
        """Intraday series when warm, daily bars when not. Never silently None."""
        live = self.series.realized_vol(symbol, lookback)
        if live is not None:
            return {"value": round(live, 4), "source": "intraday"}
        bars = self._market_bars(symbol, "1Day")
        daily = vol.realized_from_bars(bars, window)
        ewma = vol.ewma_from_bars(bars)
        if daily is None:
            return {"value": None, "source": "unavailable"}
        return {"value": round(daily, 4), "ewma": round(ewma, 4) if ewma else None,
                "source": "daily_bars", "bars": len(bars)}

    def _vol_implied(self, price, spot, strike, t_years, option_type):
        return bs.implied_vol(price, spot, strike, t_years, option_type)

    def _build_measure_handle(self, symbol, days, sigma, skew, **context):
        """Build a bounded host-side distribution with explicit provenance."""
        spot = self._market_spot(symbol)
        if sigma is None:
            rv = self._vol_realized(symbol)
            sigma = rv.get("ewma") or rv.get("value")
        if not sigma:
            raise CapabilityError(f"{symbol}: no realized volatility available")
        days = float(days)
        if not math.isfinite(days) or days <= 0:
            raise CapabilityError("distribution horizon days must be finite and positive")
        bars = self._market_bars(symbol, "1Day")
        closes = [float(b["c"]) for b in bars if b.get("c")]
        built = ms.build(spot, float(sigma), days, closes, skew=skew)
        handle = f"m_{symbol}_{len(self._measures)}"
        self._measures[handle] = built
        self._measure_context[handle] = {
            "symbol": str(symbol), "sigma": float(sigma), "days": days,
            **context,
        }
        return {"handle": handle, "spot": round(spot, 2),
                "sigma": round(float(sigma), 4), "days": round(days, 6),
                "measures": [{"name": m.name,
                              "p_up_1pct": round(m.prob(lambda s: s > spot * 1.01), 3),
                              "p_dn_1pct": round(m.prob(lambda s: s < spot * 0.99), 3),
                              "p_move_3pct": round(
                                  m.prob(lambda s: abs(s / spot - 1) > 0.03), 3)}
                             for m in built],
                **context}

    def _vol_measures(self, symbol, days, sigma=None, skew=0.15):
        """Build the three real-world measures. Returns a handle plus a summary.

        Samples stay host-side: 60,000 floats have no business crossing the pipe.
        """
        return self._build_measure_handle(
            symbol, days, sigma, skew, horizon_source="caller_supplied")

    def _vol_measures_for(self, candidate_id, sigma=None, skew=0.15):
        """Build the exact distribution horizon for one enumerated candidate.

        Expiring contracts use their close; later contracts use WINDOW_CLOSE.
        Generated code cannot accidentally evaluate a post-window option as if
        its terminal payoff were observed during the competition.
        """
        c = self._candidates.get(candidate_id)
        if c is None:
            raise CapabilityError(f"unknown candidate {candidate_id!r}; call "
                                  "options.enumerate first")
        horizon = score_horizon.candidate_horizon(
            c.expiry, dt.datetime.now(dt.timezone.utc))
        return self._build_measure_handle(
            c.underlying, horizon["score_horizon_trading_days"], sigma, skew,
            horizon_source="candidate_score_horizon", candidate=candidate_id,
            evaluation_at=horizon["evaluation_at"],
            residual_calendar_days_at_evaluation=(
                horizon["residual_calendar_days_at_evaluation"]),
            valuation_basis=horizon["valuation_basis"])

    @staticmethod
    def _candidate_value_function(c: cand.Candidate, context: dict):
        expiry_at = score_horizon.expiry_close(c.expiry)
        expected_at = score_horizon.evaluation_at(c.expiry)
        if context.get("horizon_source") == "candidate_score_horizon":
            actual_at = dt.datetime.fromisoformat(str(context.get("evaluation_at")))
            if actual_at != expected_at:
                raise CapabilityError(
                    "measure handle does not match candidate score horizon")
        if expiry_at <= WINDOW_CLOSE:
            return lambda spot: st.net_payoff_at(c.legs, spot)
        if context.get("horizon_source") != "candidate_score_horizon":
            raise CapabilityError(
                "post-window candidates must use vol.measures_for(candidate_id); "
                "expiry payoff is not score-time account value")
        return lambda spot: score_horizon.executable_value(c, spot, expected_at)

    def _vol_evaluate(self, candidate_id, measure_handle,
                      include_iv_sensitivity=True):
        """Edge under every measure, and whether the candidate survives all of them."""
        c = self._candidates.get(candidate_id)
        if c is None:
            raise CapabilityError(f"unknown candidate {candidate_id!r}; call "
                                  "options.enumerate first")
        measures = self._measures.get(measure_handle)
        if measures is None:
            raise CapabilityError(f"unknown measure handle {measure_handle!r}; call "
                                  "vol.measures first")
        traded = c.net_price * 100.0
        context = self._measure_context.get(measure_handle) or {}
        value_at_horizon = self._candidate_value_function(c, context)
        out = ms.evaluate(
            value_at_horizon, measures, traded, max_loss=c.max_loss,
            days=float(context.get("days") or 1.0))
        out["candidate"] = candidate_id
        out["evaluated_net_price"] = round(float(c.net_price), 6)
        out["max_loss"] = round(c.max_loss, 2)
        out["risk_reward"] = round(c.risk_reward, 3)
        horizon = score_horizon.candidate_horizon(
            c.expiry, dt.datetime.now(dt.timezone.utc))
        out.update({key: horizon[key] for key in (
            "evaluation_at", "score_horizon_trading_days",
            "residual_calendar_days_at_evaluation", "valuation_basis")})
        if (include_iv_sensitivity
                and score_horizon.expiry_close(c.expiry) > WINDOW_CLOSE):
            at = score_horizon.evaluation_at(c.expiry)
            out["score_horizon_iv_sensitivity"] = {
                f"iv_{int(multiplier * 100)}pct": round(
                    sum(m.expected(lambda spot, mult=multiplier:
                                   score_horizon.executable_value(
                                       c, spot, at, iv_multiplier=mult))
                        for m in measures) / len(measures) - traded, 4)
                for multiplier in (0.8, 1.0, 1.2)
            }
        self._evaluated.append({"candidate": candidate_id,
                                "signature": self._candidate_signature(c),
                                "handle": measure_handle,
                                "result": dict(out)})
        return out

    def _vol_evaluate_many(self, candidate_ids, measure_handle,
                           include_iv_sensitivity=False):
        """Evaluate a program-owned candidate batch without one RPC per row.

        The broad pass omits the three additional IV-rescoring sweeps by default.
        After ranking, call ``vol.evaluate`` once on the finalist to attach that
        relatively expensive sensitivity evidence before staging.
        """
        ids = list(dict.fromkeys(str(candidate_id) for candidate_id in candidate_ids))
        if not ids:
            raise CapabilityError("candidate_ids must not be empty")
        return {candidate_id: self._vol_evaluate(
                    candidate_id, measure_handle,
                    include_iv_sensitivity=bool(include_iv_sensitivity))
                for candidate_id in ids}

    def _vol_rank(self, candidate_ids, measure_handle, top_k=3):
        """Rank stability across measures. A candidate that leads under only one
        convenient distribution is a modelling artifact."""
        measures = self._measures.get(measure_handle)
        if measures is None:
            raise CapabilityError(f"unknown measure handle {measure_handle!r}")
        rows = [{"id": cid} for cid in candidate_ids if cid in self._candidates]
        if not rows:
            raise CapabilityError("none of those candidate ids are known")
        cached: dict[str, dict] = {}
        wanted = {row["id"] for row in rows}
        for evaluated in reversed(self._evaluated):
            candidate_id = evaluated.get("candidate")
            if (candidate_id in wanted and candidate_id not in cached
                    and evaluated.get("handle") == measure_handle):
                cached[candidate_id] = evaluated["result"]
        measure_names = [measure.name for measure in measures]
        if (len(cached) == len(rows)
                and all(set(result.get("capital_day_score_by_measure") or {})
                        == set(measure_names) for result in cached.values())):
            # `vol.evaluate_many` already computed the exact per-measure capital
            # scores.  Re-running every Black-Scholes score-horizon payoff merely
            # to sort them doubles a broad scan's cost.
            ranks: dict[str, list[int]] = {candidate_id: [] for candidate_id in wanted}
            scores: dict[str, list[float]] = {candidate_id: [] for candidate_id in wanted}
            tops = []
            for name in measure_names:
                ordered = sorted(wanted, key=lambda candidate_id:
                    cached[candidate_id]["capital_day_score_by_measure"][name],
                    reverse=True)
                tops.append(set(ordered[:top_k]))
                for position, candidate_id in enumerate(ordered):
                    ranks[candidate_id].append(position)
                    scores[candidate_id].append(float(
                        cached[candidate_id]["capital_day_score_by_measure"][name]))
            common = set.intersection(*tops) if tops else set()
            union = set.union(*tops) if tops else set()
            out = {
                "basis": "expected_profit_per_max_loss_day",
                "ranks": ranks,
                "score_median": {candidate_id: round(sorted(values)[len(values) // 2], 6)
                                 for candidate_id, values in scores.items()},
                "stable_top": sorted(common),
                "stability": round(len(common) / len(union), 3) if union else 0.0,
            }
        else:
            context = self._measure_context.get(measure_handle) or {}
            payoff_of = lambda r: self._candidate_value_function(
                self._candidates[r["id"]], context)
            price_of = lambda r: self._candidates[r["id"]].net_price * 100.0
            max_loss_of = lambda r: self._candidates[r["id"]].max_loss
            days_of = lambda _r: float(context.get("days") or 1.0)
            out = ms.rank_stability(
                rows, measures, payoff_of, price_of, top_k=top_k,
                max_loss_of=max_loss_of, days_of=days_of)
        ranked = getattr(self, "_ranked", None)
        if ranked is None:
            self._ranked = ranked = []
        ranked.append({
            "handle": measure_handle,
            "signatures": {self._candidate_signature(self._candidates[r["id"]])
                           for r in rows},
            "candidate_count": len(rows),
            "result": dict(out),
        })
        return out

    # ---- risk --------------------------------------------------------------
    def _risk_max_loss(self, legs, net_price, qty=1):
        return st.max_loss([_leg_from_dict(l) for l in legs], net_price, qty)

    def _risk_max_profit(self, legs, net_price, qty=1):
        v = st.max_profit([_leg_from_dict(l) for l in legs], net_price, qty)
        return None if v == st.UNBOUNDED else v

    def _risk_exposure(self):
        return {"open_positions": len(self.open_positions),
                "premium_at_risk": self.open_premium_at_risk,
                "realised_loss": self.realised_loss, "equity": self.equity,
                # Aggregate risk is insufficient for management: generated code
                # must be able to name the exact durable structure it wants to
                # close.  These are host-resolved rows, not model-supplied ids.
                "structures": copy.deepcopy(self.open_positions)}

    def _risk_structures(self):
        """Current normalized structures, including actionable structure ids."""
        return copy.deepcopy(self.open_positions)

    def _risk_direction(self, candidate_id, sigma, days):
        """Host-computed directional facts for a deterministically priced candidate."""
        c = self._candidates.get(candidate_id)
        if c is None:
            raise CapabilityError(f"unknown candidate {candidate_id!r}; call "
                                  "options.enumerate first")
        sigma, days = float(sigma), float(days)
        if not math.isfinite(sigma) or sigma <= 0:
            raise CapabilityError("sigma must be a finite positive annualized value")
        if not math.isfinite(days) or days <= 0:
            raise CapabilityError("days must be a finite positive value")
        spot = float(self._market_spot(c.underlying))
        expected_move = spot * sigma * math.sqrt(days / 252.0)
        break_evens = st.breakevens(c.legs, c.net_price)
        distances = [{"price": round(price, 4),
                      "points_from_spot": round(price - spot, 4),
                      "expected_moves_from_spot": round(
                          (price - spot) / expected_move, 4)}
                     for price in break_evens]
        nearest = min(distances, key=lambda row: abs(row["points_from_spot"])) \
            if distances else None
        net_delta = c.detail.get("net_delta")
        if net_delta is None:
            candidate_bias = "unknown"
        elif float(net_delta) > 0.05:
            candidate_bias = "bullish"
        elif float(net_delta) < -0.05:
            candidate_bias = "bearish"
        else:
            candidate_bias = "neutral"

        family = str(c.family).lower()
        if family in ("vertical_call", "vertical_put"):
            directionality = "direction-led"
        elif family in ("straddle", "strangle"):
            directionality = "volatility-led"
        elif family in ("iron_condor", "iron_butterfly", "butterfly"):
            directionality = ("mixed" if net_delta is not None
                              and abs(float(net_delta)) > 0.15 else "volatility-led")
        else:
            directionality = "mixed"

        checked_context = next((row["result"] for row in reversed(
            getattr(self, "_directional_context_checked", []))
            if row["symbol"] == c.underlying.upper()), None)
        if checked_context is None:
            try:
                checked_context = self.series.directional_contexts(
                    {"SPY", "QQQ", "IWM", c.underlying},
                    dt.datetime.now(dt.timezone.utc))[c.underlying.upper()]
            except (AttributeError, KeyError):
                checked_context = None
        market_label = ((checked_context or {}).get("classification")
                        or "insufficient_data")
        if directionality == "volatility-led" or candidate_bias == "neutral":
            alignment = "neutral"
        elif candidate_bias == "unknown" or market_label == "insufficient_data":
            alignment = "insufficient_data"
        elif market_label not in ("bullish", "bearish"):
            alignment = "neutral"
        elif candidate_bias == market_label:
            alignment = "aligned"
        else:
            alignment = "conflicted"

        scenario_moves = {
            "down_1_expected_move": -expected_move,
            "down_half_expected_move": -0.5 * expected_move,
            "unchanged": 0.0,
            "up_half_expected_move": 0.5 * expected_move,
            "up_1_expected_move": expected_move,
        }
        expiry_scenarios = {
            label: {
                "underlying_price": round(max(spot + move, 0.01), 4),
                "pnl_per_unit": round(
                    st.net_payoff_at(c.legs, max(spot + move, 0.01))
                    - c.net_price * CONTRACT_MULTIPLIER, 2),
            }
            for label, move in scenario_moves.items()
        }
        horizon = score_horizon.candidate_horizon(
            c.expiry, dt.datetime.now(dt.timezone.utc))
        evaluation_at = dt.datetime.fromisoformat(horizon["evaluation_at"])
        if score_horizon.expiry_close(c.expiry) > WINDOW_CLOSE:
            score_scenarios = {
                label: {
                    "underlying_price": round(max(spot + move, 0.01), 4),
                    "pnl_per_unit": round(
                        score_horizon.executable_value(
                            c, max(spot + move, 0.01), evaluation_at)
                        - c.net_price * CONTRACT_MULTIPLIER, 2),
                }
                for label, move in scenario_moves.items()
            }
        else:
            score_scenarios = expiry_scenarios
        out = {
            "candidate": candidate_id,
            "spot": round(spot, 4),
            "sigma": round(sigma, 6),
            "days": days,
            "expected_move": round(expected_move, 4),
            "breakevens": [round(x, 4) for x in break_evens],
            "breakeven_distances": distances,
            "nearest_breakeven": nearest,
            "pnl_if_expired_now": round(
                st.net_payoff_at(c.legs, spot)
                - c.net_price * CONTRACT_MULTIPLIER, 2),
            "net_delta": net_delta,
            "dollar_delta_per_1pct": c.detail.get("dollar_delta_per_1pct"),
            "current_book_direction": self._current_book_direction(),
            "candidate_bias": candidate_bias,
            "directionality": directionality,
            "market_direction": copy.deepcopy(checked_context),
            "market_context_evidence_recorded": any(
                row["symbol"] == c.underlying.upper()
                for row in getattr(self, "_directional_context_checked", [])),
            "directional_alignment": alignment,
            "expiry_pnl_scenarios": expiry_scenarios,
            "score_horizon_pnl_scenarios": score_scenarios,
            "evaluation_at": horizon["evaluation_at"],
            "residual_calendar_days_at_evaluation": (
                horizon["residual_calendar_days_at_evaluation"]),
            "valuation_basis": horizon["valuation_basis"],
        }
        checked = getattr(self, "_direction_checked", None)
        if checked is None:
            self._direction_checked = checked = []
        checked.append({
            "candidate": candidate_id, "signature": self._candidate_signature(c),
            "sigma": sigma, "days": days, "result": dict(out)})
        return out

    @staticmethod
    def _legs_signature(underlying: str, family: str, legs) -> tuple:
        return (str(underlying), str(family), tuple(sorted(
            (str(leg.symbol), int(leg.ratio_qty), str(leg.side),
             str(leg.position_intent)) for leg in legs)))

    @classmethod
    def _candidate_signature(cls, candidate: cand.Candidate) -> tuple:
        return cls._legs_signature(candidate.underlying, candidate.family,
                                   candidate.legs)

    def _missing_entry_evidence(self, intent: TradeIntent) -> tuple[str | None, list[str]]:
        """Return candidate id and repairable omissions for this exact intent."""
        signature = self._legs_signature(intent.underlying, intent.family, intent.legs)
        candidates = [c for c in self._candidates.values()
                      if self._candidate_signature(c) == signature]
        candidate_id = candidates[-1].id if candidates else None
        missing: list[str] = []
        if signature not in self._enumerated or candidate_id is None:
            missing.append("options.enumerate must produce the exact staged structure")
            return candidate_id, missing

        evaluations = [row for row in self._evaluated
                       if row["signature"] == signature]
        if not evaluations:
            missing.append(f"vol.evaluate({candidate_id}, measure_handle)")

        evaluation_handles = {row["handle"] for row in evaluations}
        ranked = [row for row in self._ranked
                  if (signature in row["signatures"]
                      and row["candidate_count"] >= 2
                      and row["handle"] in evaluation_handles)]
        if not ranked:
            missing.append(
                f"vol.rank(candidate_ids including {candidate_id}, measure_handle) "
                "with at least two candidates, using a handle that evaluated it")

        # Direction must use the same sigma/horizon as an evaluation of this
        # structure; an unrelated direction call is not admissible evidence.
        compatible = False
        for direction in self._direction_checked:
            if direction["signature"] != signature:
                continue
            for evaluation in evaluations:
                context = self._measure_context.get(evaluation["handle"]) or {}
                if (context.get("symbol") == intent.underlying
                        and math.isclose(direction["sigma"], context.get("sigma", -1),
                                         rel_tol=1e-9, abs_tol=1e-12)
                        and math.isclose(direction["days"], context.get("days", -1),
                                         rel_tol=1e-9, abs_tol=1e-12)):
                    compatible = True
                    break
        if not compatible:
            missing.append(
                f"risk.direction({candidate_id}, sigma, days) using the evaluated "
                "measure's sigma and horizon")

        context_covered = any(
            row["symbol"] == intent.underlying.upper()
            for row in getattr(self, "_directional_context_checked", []))
        if not context_covered:
            missing.append(
                f"market.directional_context({intent.underlying!r}) in this program")

        if self.trigger.get("name") == "relevant_news":
            covered = any(query is None or intent.underlying.upper() in query
                          for query in self._news_queries)
            if not covered:
                missing.append(
                    f"market.news({intent.underlying!r}) or an unfiltered market.news() "
                    "because relevant_news triggered this cycle")
        return candidate_id, missing

    def entry_evidence(self, intent: TradeIntent) -> dict:
        """Compact host-recorded evidence for a clean confirmation context."""
        signature = self._legs_signature(intent.underlying, intent.family, intent.legs)
        evaluations = [row for row in self._evaluated
                       if row["signature"] == signature]
        directions = [row for row in self._direction_checked
                      if row["signature"] == signature]
        evaluation_handles = {row["handle"] for row in evaluations}
        rankings = [row for row in self._ranked
                    if signature in row["signatures"]
                    and row["handle"] in evaluation_handles]
        return {
            "evaluation": evaluations[-1]["result"] if evaluations else None,
            "direction": directions[-1]["result"] if directions else None,
            "directional_context": next((row["result"] for row in reversed(
                getattr(self, "_directional_context_checked", []))
                if row["symbol"] == intent.underlying.upper()), None),
            "ranking": rankings[-1]["result"] if rankings else None,
            "news_review": self._news_rows if self._news_reviewed else None,
            "scheduled_events": copy.deepcopy(self.scheduled_events),
            "current_scenario_breached": self.current_scenario_breached,
        }

    def _thesis_policy_issues(self, intent: TradeIntent,
                              candidate_id: str) -> list[str]:
        """Reject a thesis whose exits/evidence describe a different trade.

        The confirmation prompt remains useful for judgement, but these are
        categorical inconsistencies that must never depend on the model noticing
        its own earlier wording.
        """
        thesis = self.theses.get(intent.thesis_id)
        candidate = self._candidates.get(candidate_id)
        if thesis is None or candidate is None:
            return ["thesis or exact candidate is unavailable"]
        issues: list[str] = []
        thesis_underlying = str(getattr(thesis, "underlying", "") or "").upper()
        if thesis_underlying and thesis_underlying != intent.underlying.upper():
            issues.append(
                f"thesis underlying {thesis_underlying} does not match "
                f"intent underlying {intent.underlying.upper()}")
        if candidate_id not in set(thesis.evidence_refs or []):
            issues.append(
                f"thesis evidence_refs must include exact candidate {candidate_id}")
        intent_signature = self._legs_signature(
            intent.underlying, intent.family, intent.legs)
        for position in self.open_positions:
            legs = position.get("legs") or []
            position_signature = (
                str(position.get("underlying")), str(position.get("family")),
                tuple(sorted((str(leg.get("symbol")),
                              int(leg.get("ratio_qty", 1)),
                              str(leg.get("side")),
                              str(leg.get("position_intent")))
                             for leg in legs)))
            if position_signature == intent_signature:
                issues.append(
                    "the exact structure is already open; do not duplicate it")
                break
        if not str(thesis.exit_news or "").strip():
            issues.append("thesis must state a news invalidation condition")

        direction = next((row["result"] for row in reversed(self._direction_checked)
                          if row["signature"] == intent_signature), None)
        if direction:
            directionality = direction.get("directionality")
            alignment = direction.get("directional_alignment")
            budget_fraction = intent.risk_budget / self.equity if self.equity > 0 else 1.0
            if directionality == "direction-led":
                if alignment == "conflicted":
                    issues.append(
                        "direction-led candidate conflicts with current host-observed "
                        "market direction; select an aligned or volatility-led structure")
                elif (alignment in ("neutral", "insufficient_data")
                      and budget_fraction > 0.0075):
                    issues.append(
                        "direction-led candidate without aligned market evidence must cap "
                        "requested risk at 0.75% of equity")
                elif alignment == "aligned" and budget_fraction > 0.03:
                    issues.append(
                        "aligned direction-led candidate must cap requested risk at 3% "
                        "of equity")
            elif directionality == "mixed":
                if alignment == "conflicted":
                    issues.append(
                        "mixed candidate conflicts with current market direction; select "
                        "a non-conflicted or genuinely volatility-led structure")
                elif (alignment in ("neutral", "insufficient_data")
                      and budget_fraction > 0.0075):
                    issues.append(
                        "mixed candidate without aligned market evidence must cap requested "
                        "risk at 0.75% of equity")
                elif alignment == "aligned" and budget_fraction > 0.03:
                    issues.append(
                        "aligned mixed candidate must cap requested risk at 3% of equity")

        exit_at = str(getattr(thesis, "exit_at", "") or "")
        if not exit_at:
            issues.append("exit_time must be an exact YYYY-MM-DD HH:MM ET deadline")
        else:
            try:
                deadline = dt.datetime.fromisoformat(exit_at).astimezone(ET)
                earliest = min(leg.expiry for leg in intent.legs)
                latest = dt.datetime.combine(
                    earliest, dt.time(15, 45), tzinfo=ET)
                if deadline > latest:
                    issues.append(
                        "exit_time must be no later than 15:45 ET on the earliest expiry")
            except ValueError:
                issues.append("exit_time is not a parseable timezone-aware deadline")

        profit = str(thesis.exit_profit or "").lower()
        invalidation = str(thesis.exit_invalidation or "").lower()
        combined = profit + " " + invalidation
        is_debit = candidate.net_price > 0
        if is_debit:
            short_premium_phrases = (
                "entry credit", "credit received", "2x credit", "2x the credit",
                "debit to close", "buy back for")
            found = next((phrase for phrase in short_premium_phrases
                          if phrase in combined), None)
            if found:
                issues.append(
                    f"net-debit structure uses short-premium exit language: {found!r}")
            if "no drawdown stop" not in invalidation:
                issues.append(
                    "long-premium invalidation must explicitly state no drawdown stop")
        else:
            has_credit_multiple = bool(re.search(
                r"(?:2\s*x|2x|twice).{0,45}(?:credit|close)|"
                r"(?:credit|close).{0,45}(?:2\s*x|2x|twice)", invalidation))
            has_half_max_loss = bool(re.search(
                r"50\s*(?:%|pct\.?|percent)\s*(?:of\s+)?(?:the\s+)?"
                r"(?:defined\s+)?max(?:imum)?\s+loss|"
                r"half\s+(?:of\s+)?(?:the\s+)?(?:defined\s+)?"
                r"max(?:imum)?\s+loss", invalidation))
            if not (has_credit_multiple and has_half_max_loss):
                issues.append(
                    "short-premium invalidation must contain both a 2x-credit/close "
                    "stop and a 50%-of-maximum-loss stop")

        if (candidate.max_profit == st.UNBOUNDED
                and re.search(r"\b\d+(?:\.\d+)?\s*%\s+of\s+(?:the\s+)?"
                              r"max(?:imum)?\s+profit\b", profit)):
            issues.append(
                "unbounded-profit structure cannot target a percentage of maximum profit; "
                "use premium paid, structure value, or a concrete P&L target")
        if (candidate.net_price > 0 and candidate.max_profit != st.UNBOUNDED
                and "$" not in str(thesis.exit_profit or "")):
            issues.append(
                "finite-profit debit thesis must state its intended profit exit in "
                "dollars (per spread is acceptable); the host resolves total dollars "
                "from the actual fill quantity")
        return issues

    def _required_exit_policy(self, intent: TradeIntent,
                              candidate_id: str) -> dict[str, object]:
        """Canonical host policy for the exact candidate, independent of prose."""
        candidate = self._candidates[candidate_id]
        thesis = self.theses.get(intent.thesis_id)
        if candidate.net_price < 0:
            profit_target = {
                "kind": "entry_credit_fraction", "value": 0.5}
        elif candidate.max_profit != st.UNBOUNDED:
            profit_target = {
                "kind": "maximum_profit_fraction", "value": 0.5}
        else:
            # An unbounded maximum cannot be multiplied.  This remains a typed
            # entry-basis return target rather than pretending "50% of maximum"
            # has a finite meaning.
            profit_target = {
                "kind": "entry_basis_profit_fraction", "value": 0.5}
        policy: dict[str, object] = {
            "schema_version": 2,
            "candidate_id": candidate_id,
            "premium_type": "long" if candidate.net_price > 0 else "short",
            "profit_target": profit_target,
            "time_stop": str(getattr(thesis, "exit_at", "") or ""),
        }
        if candidate.net_price > 0:
            policy["drawdown_stop"] = None
        else:
            policy["loss_stops"] = [
                {"kind": "close_debit_multiple_of_entry_credit",
                 "value": float(self.params.short_premium_stop_multiple)},
                {"kind": "loss_fraction_of_defined_maximum_loss", "value": 0.5},
            ]
        return policy

    def _current_book_direction(self):
        """Current option delta aggregated from reconciled broker/ledger structures."""
        legs = [(position, leg) for position in self.open_positions
                for leg in position.get("legs", []) if leg.get("symbol")]
        if not legs:
            return {"by_underlying": {}, "total_dollar_delta_per_1pct": 0.0,
                    "missing_symbols": []}
        symbols = sorted({str(leg["symbol"]) for _, leg in legs})
        quotes = self.rest.option_quotes(symbols)
        spots: dict[str, float] = {}
        totals: dict[str, dict] = {}
        missing: list[str] = []
        now = dt.datetime.now(dt.timezone.utc)
        for position, leg in legs:
            symbol = str(leg["symbol"])
            quote = quotes.get(symbol) or {}
            bid = float(quote.get("bp", 0) or 0)
            ask = float(quote.get("ap", 0) or 0)
            if bid <= 0 or ask <= 0:
                missing.append(symbol)
                continue
            underlying = str(position.get("underlying") or "")
            if not underlying:
                missing.append(symbol)
                continue
            spot = spots.setdefault(underlying, float(self._market_spot(underlying)))
            try:
                expiry = dt.datetime.fromisoformat(str(leg["expiry"])).replace(
                    hour=16, tzinfo=ET)
                strike = float(leg["strike"])
                option_type = str(leg["option_type"])
            except (KeyError, TypeError, ValueError):
                missing.append(symbol)
                continue
            t = bs.year_fraction(now, expiry)
            mid = (bid + ask) / 2
            iv = bs.implied_vol(mid, spot, strike, t, option_type)
            if iv is None:
                missing.append(symbol)
                continue
            delta = bs.greeks(spot, strike, t, iv, option_type).delta
            signed = 1 if leg.get("side") == "buy" else -1
            units = signed * int(leg.get("ratio_qty", 1)) * int(position.get("qty", 1))
            row = totals.setdefault(underlying, {
                "net_delta": 0.0, "dollar_delta_per_1pct": 0.0})
            row["net_delta"] += units * delta
            row["dollar_delta_per_1pct"] += units * delta * spot
        rounded = {symbol: {key: round(value, 2 if key.startswith("dollar") else 4)
                            for key, value in row.items()}
                   for symbol, row in totals.items()}
        return {"by_underlying": rounded,
                "total_dollar_delta_per_1pct": round(sum(
                    row["dollar_delta_per_1pct"] for row in totals.values()), 2),
                "missing_symbols": sorted(set(missing))}

    # ---- account -----------------------------------------------------------
    def _account_state(self):
        return {"equity": self.equity, "positions": self.open_positions,
                "realised_loss": self.realised_loss,
                "premium_at_risk": self.open_premium_at_risk}

    # ---- thesis ------------------------------------------------------------
    def _thesis_open(self, hypothesis, underlying, exit_profit, exit_invalidation,
                     exit_time, exit_news="", evidence_refs=None, gates=None):
        t = self.theses.open(hypothesis, underlying, exit_profit=exit_profit,
                             exit_invalidation=exit_invalidation, exit_time=exit_time,
                             exit_news=exit_news, evidence_refs=evidence_refs,
                             gates=gates)
        return t.to_json()

    def _thesis_list(self, status="open"):
        return [t.to_json() for t in self.theses.list(status)]

    def _thesis_history(self, limit=20):
        """Closed theses and how each ended. The bundle carries a digest; this is
        the full record when a cycle wants it."""
        return self.theses.outcomes(limit)

    def _thesis_close(self, thesis_id, reason, realised=None):
        if any(str(row.get("thesis_id") or "") == str(thesis_id)
               and int(row.get("qty") or 0) > 0 for row in self.open_positions):
            return {
                "status": "deferred_until_flat", "thesis_id": str(thesis_id),
                "reason": (
                    "reconciled broker exposure remains; fill reconciliation owns "
                    "thesis closure after the structure is flat"),
            }
        return self.theses.close(thesis_id, reason=reason, realised=realised).to_json()

    def _thesis_note(self, thesis_id, note):
        return self.theses.update(thesis_id, note=note).to_json()

    # ---- trading (two-phase, gated) ---------------------------------------
    def _entry_precheck(self, ti: TradeIntent) -> tuple[dict | None, dict | None]:
        """Return a repair result or the fresh materialisation arguments."""
        if self.theses.get(ti.thesis_id) is None:
            raise CapabilityError(
                f"unknown thesis {ti.thesis_id!r}; call thesis.open before trading")
        candidate_id, missing = self._missing_entry_evidence(ti)
        if missing:
            return {"status": "needs_evidence", "candidate": candidate_id,
                    "missing": missing,
                    "next": "call the missing capabilities for this exact candidate, "
                            "reconsider their results, then retry the entry action"}, None
        revision_issues = self._thesis_policy_issues(ti, candidate_id)
        if revision_issues:
            result = {
                "status": "needs_revision", "candidate": candidate_id,
                "thesis_id": ti.thesis_id, "issues": revision_issues,
                "next": "close the incorrect thesis, open a corrected thesis for "
                        "this exact candidate, then retry the entry action or decline",
            }
            if candidate_id in self._candidates and self.theses.get(ti.thesis_id):
                result["required_exit_policy"] = self._required_exit_policy(
                    ti, candidate_id)
            return result, None
        # Persist what the host will actually enforce.  This is intentionally
        # candidate-bound and separate from the model's explanatory prose.
        self.theses.bind_exit_policy(
            ti.thesis_id, self._required_exit_policy(ti, candidate_id))
        return None, self._entry_materialise_kwargs(ti)

    def _trading_execute(self, intent: dict):
        if self._program_decision is not None:
            raise CapabilityError(
                f"program already ended with {self._program_decision['status']!r}; "
                "do not trade after selecting a decision outcome")
        ti = _intent_from_dict(intent)
        out, kw = self._entry_precheck(ti)
        if out is not None:
            self._trading_result = dict(out)
            return out
        if self.ex.mode == "execute":
            out = {
                "status": "needs_price_authorization",
                "next": "live entries must call trading.execute_if with exactly one "
                        "explicit max_entry_debit or min_entry_credit; choose the worst "
                        "fresh executable price the thesis still accepts",
            }
            self._trading_result = dict(out)
            return out
        out = self.ex.execute(ti, **(kw or {}))
        self._trading_result = dict(out)
        if out.get("status") == "submitted":
            self._submitted_this_program = True
            thesis = self.theses.get(ti.thesis_id)
            if thesis is not None:
                self.theses.update(ti.thesis_id, order_ids=[out["order_id"]])
        return out

    def _trading_execute_if(self, intent: dict, max_entry_debit=None,
                            min_entry_credit=None, valid_for_seconds=30):
        """Two-phase entry whose reviewed price boundary survives decision lag."""
        if self._program_decision is not None:
            raise CapabilityError(
                f"program already ended with {self._program_decision['status']!r}")
        ti = _intent_from_dict(intent)
        out, kw = self._entry_precheck(ti)
        if out is not None:
            self._trading_result = dict(out)
            return out
        try:
            condition = entry_condition(
                max_entry_debit=max_entry_debit,
                min_entry_credit=min_entry_credit)
            out = self.ex.execute(
                ti, economic_condition=condition,
                authorization_seconds=float(valid_for_seconds), **(kw or {}))
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        self._trading_result = dict(out)
        if out.get("status") == "submitted":
            self._submitted_this_program = True
            thesis = self.theses.get(ti.thesis_id)
            if thesis is not None:
                self.theses.update(ti.thesis_id, order_ids=[out["order_id"]])
        return out

    def _trading_preview(self, intent: dict):
        ti = _intent_from_dict(intent)
        staged = self.ex.materialise(ti, **self._entry_materialise_kwargs(ti),
                                     store=False)
        maximum_profit = staged.verified.max_profit
        return {"qty": staged.verified.qty, "limit_price": staged.verified.limit_price,
                "max_loss": staged.verified.max_loss,
                "max_profit": None if maximum_profit == st.UNBOUNDED else maximum_profit,
                "sizing": staged.sizing,
                "risk_reward": (maximum_profit / staged.verified.max_loss
                                if staged.verified.max_loss > 0
                                and maximum_profit != st.UNBOUNDED else None),
                "passed": staged.passed,
                "checklist": staged.checklist()}

    def _entry_materialise_kwargs(self, intent: TradeIntent) -> dict:
        underlyings = {intent.underlying.upper()} | {
            str(row.get("underlying") or "").upper()
            for row in self.open_positions if row.get("underlying")}
        spots = {}
        for symbol in sorted(underlyings):
            try:
                spots[symbol] = float(self._market_spot(symbol))
            except Exception:
                # The portfolio-scenario gate names the missing spot and fails
                # closed; evidence construction must not hide the omission.
                continue
        return dict(
            equity=self.equity,
            open_premium_at_risk=self.open_premium_at_risk,
            realised_loss=self.realised_loss,
            open_positions=self.open_positions,
            entry_evidence=self.entry_evidence(intent),
            market_spots=spots,
        )

    def _trading_close(self, structure_id: str, reason: str):
        """Close one host-reconciled structure in full.

        Exits are risk-reducing and therefore do not use the entry staging flow,
        but the model may only name an exact structure the host supplied. Pricing,
        closing intents, short-leg-first ordering, dedupe, and submission remain
        entirely host-owned.
        """
        if self._program_decision is not None:
            raise CapabilityError(
                f"program already ended with {self._program_decision['status']!r}; "
                "do not trade after selecting a decision outcome")
        if self._submitted_this_program:
            raise CapabilityError("an order was already submitted in this program")
        reason = str(reason or "").strip()
        if not reason:
            raise CapabilityError("closing reason must be non-empty")
        structure = next((p for p in self.open_positions
                          if str(p.get("structure_id")) == str(structure_id)), None)
        if structure is None:
            raise CapabilityError(f"unknown open structure {structure_id!r}")
        mandatory = getattr(self, "trigger", {}).get(
            "name") == "portfolio_scenario_breach"
        out = self.ex.close_structure(
            structure, reason=reason, now=dt.datetime.now(ET),
            must_fill=mandatory,
            mandatory_source=("portfolio_scenario_breach" if mandatory else ""))
        self._trading_result = dict(out)
        if out.get("status") in ("submitted_close", "unknown"):
            self._submitted_this_program = True
        return out

    def _trading_close_if(self, structure_id: str, min_executable_profit,
                          reason: str):
        """Close now only if a fresh executable whole-structure P&L still qualifies."""
        if self._program_decision is not None:
            raise CapabilityError(
                f"program already ended with {self._program_decision['status']!r}")
        if self._submitted_this_program:
            raise CapabilityError("an order was already submitted in this program")
        structure = next((row for row in self.open_positions
                          if str(row.get("structure_id")) == str(structure_id)), None)
        if structure is None:
            raise CapabilityError(f"unknown open structure {structure_id!r}")
        reason = str(reason or "").strip()
        if not reason:
            raise CapabilityError("closing reason must be non-empty")
        out = self.ex.close_structure(
            structure, reason=reason, now=dt.datetime.now(ET),
            min_executable_profit=float(min_executable_profit))
        self._trading_result = dict(out)
        if out.get("status") in ("submitted_close", "unknown"):
            self._submitted_this_program = True
        return out

    def _trading_set_entry_trigger(self, intent: dict, max_entry_debit=None,
                                   min_entry_credit=None, valid_for_seconds=60,
                                   max_spot_drift_pct=0.3, reason=""):
        """Authorize one exact entry briefly; Tier 0 re-runs all host gates."""
        if self.action_triggers is None:
            raise CapabilityError("action trigger store is unavailable")
        if self._submitted_this_program:
            raise CapabilityError("a market action was already authorized in this program")
        ti = _intent_from_dict(intent)
        out, _ = self._entry_precheck(ti)
        if out is not None:
            self._trading_result = dict(out)
            return out
        try:
            condition = entry_condition(
                max_entry_debit=max_entry_debit,
                min_entry_credit=min_entry_credit)
            row = self.action_triggers.set_entry(
                ti, condition=condition,
                valid_for_seconds=float(valid_for_seconds),
                reference_spot=float(self._market_spot(ti.underlying)),
                max_spot_drift_pct=float(max_spot_drift_pct),
                evidence=self.entry_evidence(ti), reason=str(reason))
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        out = {**row, "trigger_status": row.get("status"),
               "status": "trigger_armed"}
        self._trading_result = dict(out)
        self._submitted_this_program = True
        return out

    def _trading_set_exit_trigger(self, structure_id: str,
                                  min_executable_profit=None, spot_above=None,
                                  spot_below=None, valid_for_seconds=3600,
                                  confirmation_samples=2,
                                  sample_interval_seconds=10, reason=""):
        """Authorize a removable one-shot close on profit or confirmed spot."""
        if self.action_triggers is None:
            raise CapabilityError("action trigger store is unavailable")
        structure = next((row for row in self.open_positions
                          if str(row.get("structure_id")) == str(structure_id)), None)
        if structure is None:
            raise CapabilityError(f"unknown open structure {structure_id!r}")
        try:
            row = self.action_triggers.set_exit(
                str(structure_id),
                min_executable_profit=(float(min_executable_profit)
                                       if min_executable_profit is not None else None),
                spot_above=(float(spot_above) if spot_above is not None else None),
                spot_below=(float(spot_below) if spot_below is not None else None),
                underlying=str(structure.get("underlying") or ""),
                confirmation_samples=int(confirmation_samples),
                sample_interval_seconds=float(sample_interval_seconds),
                valid_for_seconds=float(valid_for_seconds), reason=str(reason))
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        out = {**row, "trigger_status": row.get("status"),
               "status": "trigger_armed"}
        self._trading_result = dict(out)
        self._submitted_this_program = True
        return out

    def _trading_remove_trigger(self, trigger_id: str, reason: str):
        if self.action_triggers is None:
            raise CapabilityError("action trigger store is unavailable")
        try:
            row = self.action_triggers.remove(str(trigger_id), str(reason))
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        out = {**row, "trigger_status": row.get("status"),
               "status": "trigger_removed"}
        self._trading_result = dict(out)
        return out

    def _trading_list_triggers(self):
        if self.action_triggers is None:
            return []
        return self.action_triggers.active()

    def _trading_authorize_settlement(self, structure_id: str,
                                      min_short_distance_points,
                                      reason: str):
        """Permit a defined-risk expiry position to remain after 15:15 conditionally."""
        if self.settlement_authorizations is None:
            raise CapabilityError("settlement authorization store is unavailable")
        if self._submitted_this_program:
            raise CapabilityError("a market action was already authorized in this program")
        structure = next((row for row in self.open_positions
                          if str(row.get("structure_id")) == str(structure_id)), None)
        if structure is None:
            raise CapabilityError(f"unknown open structure {structure_id!r}")
        try:
            row = self.settlement_authorizations.authorize(
                str(structure_id),
                min_short_distance_points=float(min_short_distance_points),
                reason=str(reason))
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        out = {**row, "status": "settlement_authorized"}
        self._trading_result = dict(out)
        self._submitted_this_program = True
        return out

    def _trading_remove_settlement_authorization(self, structure_id: str,
                                                  reason: str):
        if self.settlement_authorizations is None:
            raise CapabilityError("settlement authorization store is unavailable")
        try:
            row = self.settlement_authorizations.remove(
                str(structure_id), reason=str(reason))
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
        return {"status": "settlement_authorization_removed", **row}

    def _trading_list_settlement_authorizations(self):
        if self.settlement_authorizations is None:
            return []
        return self.settlement_authorizations.observable()

    def _trading_set_exit_policy(self, structure_id: str, activation_profit,
                                 max_profit_giveback, minimum_locked_profit=0,
                                 confirmation_samples=2, reason=""):
        """Delegate a monotonic executable-profit trail to Tier 0."""
        if self.exit_policies is None:
            raise CapabilityError("adaptive exit policy store is unavailable")
        structure = next((row for row in self.open_positions
                          if str(row.get("structure_id")) == str(structure_id)), None)
        if structure is None:
            raise CapabilityError(f"unknown open structure {structure_id!r}")
        target = structure.get("profit_target")
        if target is None:
            basis = abs(float(structure.get("cost_basis") or 0))
            target = basis * self.params.profit_target_pct / 100.0
        try:
            return self.exit_policies.set(
                str(structure_id), activation_profit=float(activation_profit),
                max_profit_giveback=float(max_profit_giveback),
                minimum_locked_profit=float(minimum_locked_profit),
                confirmation_samples=int(confirmation_samples),
                hard_profit_target=float(target or 0), reason=str(reason))
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
