"""Two-phase execution.

The model proposes a TradeIntent -- geometry only. The host materialises a
VerifiedTradeIntent from fresh quotes and account state, which is the only object
execution accepts. Staging returns the rendered checklist; confirmation requires
an identical intent, a later model program, and a live nonce.

Geometry stays with the model, pricing stays with the host.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation

from agent.host import gates, portfolio_risk
from agent.host.contracts import resolve_intent
from agent.host.ledger import ExecutionLedger, TERMINAL_STATUSES
from agent.host.rest import Rest
from agent.host.risk_params import RiskParams
from agent.quant import structures as st
from agent.types import (CONTRACT_MULTIPLIER, GateResult, Leg, TradeIntent,
                         VerifiedTradeIntent)

TTL_SECONDS = 45.0
EXACT_RETRY_MAX_AGE_SECONDS = 45.0


class StagedOrder:
    def __init__(self, verified: VerifiedTradeIntent, results: list[GateResult],
                 staged_program_id: int | None = None, sizing: dict | None = None,
                 economic_condition: dict | None = None,
                 authorization_deadline: dt.datetime | None = None):
        self.verified = verified
        self.results = results
        self.sizing = sizing or {}
        self.economic_condition = dict(economic_condition or {}) or None
        self.authorization_deadline = authorization_deadline
        # Confirmation is a deliberation boundary, not merely a second function
        # call.  The host records which model program created the draft so two
        # execute() calls in one generated program can never submit it.
        self.staged_program_id = staged_program_id

    @property
    def passed(self) -> bool:
        return gates.all_passed(self.results)

    def checklist(self) -> str:
        v = self.verified
        head = (f"{v.intent.underlying} {v.intent.family}  qty {v.qty} @ "
                f"{'debit' if v.limit_price > 0 else 'credit'} {abs(v.limit_price):.2f}\n"
                f"max loss ${v.max_loss:,.0f}   max profit "
                f"{'unbounded' if v.max_profit == st.UNBOUNDED else f'${v.max_profit:,.0f}'}\n")
        if self.sizing:
            head += (f"binding constraint {self.sizing.get('binding_constraint')} · "
                     f"requested {self.sizing.get('requested_qty')} · "
                     f"allowed {self.sizing.get('allowed_qty')}\n")
        return head + "\n" + gates.render(self.results)

    @property
    def economic_condition_passed(self) -> bool:
        rows = [row for row in self.results if row.name == "economic_condition"]
        return not rows or all(row.passed for row in rows)

    def confirmation_call(self) -> dict:
        """Exact capability recipe required to confirm this canonical draft."""
        if not self.economic_condition:
            return {
                "namespace": "trading", "function": "execute",
                "intent": "identical canonical_staged_order",
                "kwargs": {},
            }
        kind = str(self.economic_condition["kind"])
        seconds = None
        if self.authorization_deadline is not None:
            seconds = max(5.0, min(120.0, (
                self.authorization_deadline - self.verified.materialised_at
            ).total_seconds()))
        kwargs = {kind: float(self.economic_condition["value"])}
        if seconds is not None:
            kwargs["valid_for_seconds"] = int(seconds) if seconds.is_integer() else seconds
        return {
            "namespace": "trading", "function": "execute_if",
            "intent": "identical canonical_staged_order",
            "kwargs": kwargs,
            "authorization_deadline": (
                self.authorization_deadline.isoformat()
                if self.authorization_deadline is not None else None),
            "warning": (
                "repeat execute_if with the identical intent and boundary; "
                "switching to trading.execute cannot confirm this draft"),
        }


def _quote_hash(quotes: dict[str, dict]) -> str:
    blob = json.dumps({k: [v.get("bp"), v.get("ap")] for k, v in sorted(quotes.items())})
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _leg_price(quote: dict, side: str) -> float:
    """Conservative: buy at the ask, sell at the bid."""
    return float(quote["ap"] if side == "buy" else quote["bp"])


def _fresh_price_edge_gate(intent: TradeIntent, quotes: dict[str, dict],
                           fresh_net: float, entry_evidence: dict | None,
                           multiplier: float = 1.5) -> tuple[GateResult, dict]:
    """Reprice the weakest expected profit and compare it with live friction.

    Model evaluation owns the payoff distribution; the host owns the executable
    entry price.  Expected profit changes dollar-for-dollar with the latter, so a
    later quote never inherits a stale positive edge merely because the geometry
    stayed the same.
    """
    evaluation = (entry_evidence or {}).get("evaluation") or {}
    expected = evaluation.get("expected_profit_by_measure") or {}
    evaluated_net = evaluation.get("evaluated_net_price")
    try:
        old_net = float(evaluated_net)
        values = {str(name): float(value) for name, value in expected.items()}
    except (TypeError, ValueError):
        values = {}
        old_net = 0.0
    if not values or evaluated_net is None:
        detail = {"status": "incomplete", "reason": (
            "evaluation lacks expected_profit_by_measure or evaluated_net_price")}
        return GateResult("fresh_price_edge", False, detail["reason"]), detail
    half_spread = 0.0
    missing = []
    for leg in intent.legs:
        quote = quotes.get(leg.symbol) or {}
        try:
            bid, ask = float(quote["bp"]), float(quote["ap"])
        except (KeyError, TypeError, ValueError):
            missing.append(leg.symbol)
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            missing.append(leg.symbol)
            continue
        half_spread += abs(int(leg.ratio_qty)) * (ask - bid) / 2.0
    if missing:
        detail = {"status": "incomplete", "missing_symbols": missing}
        return GateResult(
            "fresh_price_edge", False,
            f"live half-spread is unavailable for {', '.join(missing)}"), detail
    adjustment = (old_net - float(fresh_net)) * CONTRACT_MULTIPLIER
    repriced = {name: value + adjustment for name, value in values.items()}
    weakest_name, weakest = min(repriced.items(), key=lambda item: item[1])
    round_trip = 2.0 * half_spread * CONTRACT_MULTIPLIER
    floor = float(multiplier) * round_trip
    passed = weakest + 1e-9 >= floor
    detail = {
        "status": "ok" if passed else "refused",
        "evaluated_net_price": round(old_net, 4),
        "fresh_executable_net_price": round(float(fresh_net), 4),
        "price_adjustment_per_spread": round(adjustment, 2),
        "expected_profit_by_measure_repriced": {
            key: round(value, 4) for key, value in repriced.items()},
        "weakest_measure": weakest_name,
        "weakest_expected_profit": round(weakest, 4),
        "round_trip_half_spread_cost": round(round_trip, 4),
        "required_multiple": float(multiplier),
        "required_expected_profit": round(floor, 4),
    }
    reason = (f"{weakest_name} fresh expected profit ${weakest:.2f} must clear "
              f"{multiplier:.1f}x live round-trip half-spread cost "
              f"${round_trip:.2f} = ${floor:.2f}; evaluation net "
              f"{old_net:.2f}, fresh executable net {fresh_net:.2f}")
    return GateResult("fresh_price_edge", passed, reason), detail


def _economic_condition_gate(net: float, condition: dict | None, *,
                             priceable: bool, now: dt.datetime,
                             deadline: dt.datetime | None) -> GateResult | None:
    if not condition:
        return None
    kind = str(condition.get("kind") or "")
    value = float(condition.get("value") or 0)
    if deadline is None or now > deadline:
        return GateResult("economic_condition", False,
                          "authorization expired before fresh-price submission")
    if not priceable:
        return GateResult("economic_condition", False,
                          "fresh executable price is unavailable")
    if kind == "max_entry_debit":
        passed = net > 0 and net <= value + 1e-9
        return GateResult(
            "economic_condition", passed,
            f"fresh debit {net:.2f} must be <= {value:.2f}; "
            f"authorization expires {deadline.isoformat()}")
    if kind == "min_entry_credit":
        credit = -net
        passed = net < 0 and credit + 1e-9 >= value
        return GateResult(
            "economic_condition", passed,
            f"fresh credit {credit:.2f} must be >= {value:.2f}; "
            f"authorization expires {deadline.isoformat()}")
    return GateResult("economic_condition", False,
                      f"unsupported economic condition {kind!r}")


def _decimal_text(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    text = format(number.normalize(), "f")
    return "0" if text in ("-0", "") else text


def canonical_order_request(raw: dict) -> dict:
    """Normalize request semantics so repr/json ordering cannot affect adoption."""
    order_class = str(raw.get("order_class") or "simple").lower()
    if order_class == "mleg":
        source_legs = raw.get("legs") or []
    else:
        source_legs = [{"symbol": raw.get("symbol"), "ratio_qty": 1,
                        "side": raw.get("side"),
                        "position_intent": raw.get("position_intent")}]
    legs = sorted(
        [{"symbol": str(l.get("symbol") or "").upper(),
          "ratio_qty": int(float(l.get("ratio_qty") or 1)),
          "side": str(l.get("side") or "").lower(),
          "position_intent": str(l.get("position_intent") or "").lower()}
         for l in source_legs],
        key=lambda l: (l["symbol"], l["side"], l["position_intent"], l["ratio_qty"]))
    return {
        "order_class": order_class,
        "qty": _decimal_text(raw.get("qty")),
        "type": str(raw.get("type") or raw.get("order_type") or "").lower(),
        "limit_price": _decimal_text(raw.get("limit_price")),
        "time_in_force": str(raw.get("time_in_force") or "").lower(),
        "client_order_id": str(raw.get("client_order_id") or ""),
        "legs": legs,
    }


def canonical_broker_order(order: dict) -> dict:
    """Project an Alpaca order response onto the fields sent in the request."""
    raw = dict(order)
    if str(raw.get("order_class") or "").lower() != "mleg" and not raw.get("legs"):
        raw["order_class"] = "simple"
    return canonical_order_request(raw)


class Executor:
    def __init__(self, rest: Rest, params: RiskParams, profile_name: str,
                 mode: str = "propose", ledger: ExecutionLedger | None = None,
                 expected_account_id: str | None = None,
                 enforce_entry_risk: bool = False):
        self.rest = rest
        self.params = params
        self.profile_name = profile_name
        self.mode = mode                     # "propose" | "execute"
        self.ledger = ledger
        profile = getattr(rest, "profile", None)
        self.expected_account_id = expected_account_id or getattr(
            profile, "expected_account_id", None)
        self._staged: dict[str, StagedOrder] = {}
        self._consumed: set[str] = set()
        self._terminal_intents: set[str] = set()
        self._attempt_status: dict[str, str] = {}
        self.cycle_id: str | None = None
        self.program_id: int | None = None
        self._submit_lock = threading.RLock()
        # The application entrypoint enables this explicitly.  Keeping the pure
        # executor usable without market-history dependencies is useful to probes
        # and unit tests, but production entry can never omit the evidence/stress
        # path accidentally.
        self.enforce_entry_risk = bool(enforce_entry_risk)

    def begin_cycle(self, cycle_id: str) -> None:
        """Staging is deliberately scoped to one model decision cycle."""
        self.cycle_id = cycle_id
        self.program_id = None
        self._staged.clear()
        self._terminal_intents.clear()

    def begin_program(self, program_id: int) -> None:
        """Mark the model-program boundary used by two-phase confirmation."""
        if self.cycle_id is None:
            raise RuntimeError("cannot begin a program outside a decision cycle")
        self.program_id = int(program_id)

    def end_cycle(self) -> None:
        self._staged.clear()
        self._terminal_intents.clear()
        self.cycle_id = None
        self.program_id = None

    @property
    def latest_staged(self) -> StagedOrder | None:
        return self._staged[next(reversed(self._staged))] if self._staged else None

    def discard_staged(self) -> None:
        """Discard drafts created by a failed model program."""
        self._staged.clear()

    # ---- materialisation ---------------------------------------------------
    def materialise(self, intent: TradeIntent, *, equity: float,
                    open_premium_at_risk: float = 0.0, realised_loss: float = 0.0,
                    open_positions: list | None = None,
                    entry_evidence: dict | None = None,
                    market_spots: dict[str, float] | None = None,
                    quantity_ceiling: int | None = None,
                    economic_condition: dict | None = None,
                    authorization_deadline: dt.datetime | None = None,
                    now: dt.datetime | None = None, store: bool = True) -> StagedOrder:
        intent = resolve_intent(self.rest, intent)
        open_positions = open_positions or []
        candidate_symbols = [l.symbol for l in intent.legs]
        symbols = list(dict.fromkeys(
            candidate_symbols
            + [str(leg.get("symbol")) for position in open_positions
               for leg in position.get("legs") or [] if leg.get("symbol")]))
        quotes = self.rest.option_quotes(symbols)
        account = self.rest.account()
        now = now or dt.datetime.now(dt.timezone.utc)

        results: list[GateResult] = [
            gates.g_paper_endpoint(self.rest.profile and "https://paper-api.alpaca.markets"),
            gates.g_account_identity(account, self.profile_name,
                                     self.expected_account_id or "", now=now),
            gates.g_account_tradable(account),
            gates.g_structure(intent.legs, intent.underlying),
        ]
        for sym in candidate_symbols:
            results.append(gates.g_quote_valid(sym, quotes.get(sym, {}), self.params, now=now))
            if quotes.get(sym):
                results.append(gates.g_spread(sym, quotes[sym], self.params))

        # Price from the quotes just fetched, never from anything the model supplied.
        net = 0.0
        priceable = all(quotes.get(s) for s in candidate_symbols)
        if priceable:
            for leg in intent.legs:
                net += leg.sign * leg.ratio_qty * _leg_price(quotes[leg.symbol], leg.side)

        evidence = portfolio_risk.evidence_risk_ceiling(
            entry_evidence, equity,
            robust_pct=self.params.robust_evidence_risk_pct,
            supported_pct=self.params.supported_evidence_risk_pct,
            partial_pct=self.params.partial_evidence_risk_pct)
        scheduled = (entry_evidence or {}).get("scheduled_events") or {}
        next_event = scheduled.get("next_event") or {}
        minutes_until_event = next_event.get("minutes_until")
        is_short_gamma = (
            net < 0
            and intent.family in {
                "iron_condor", "iron_butterfly", "vertical_call", "vertical_put"
            })
        event_multiplier = 1.0
        if (is_short_gamma and isinstance(minutes_until_event, (int, float))
                and 0 <= float(minutes_until_event)
                <= self.params.scheduled_event_window_minutes
                and not bool((entry_evidence or {}).get(
                    "current_scenario_breached"))):
            event_multiplier = self.params.short_gamma_event_size_multiplier
            evidence["ceiling_dollars"] *= event_multiplier
            evidence["ceiling_pct"] *= event_multiplier
        evidence["scheduled_event_multiplier"] = event_multiplier
        evidence["next_scheduled_event"] = (
            {key: next_event.get(key) for key in ("name", "at_et", "minutes_until")}
            if next_event else None)
        edge_gate = None
        if priceable and self.enforce_entry_risk:
            edge_gate, fresh_edge = _fresh_price_edge_gate(
                intent, quotes, net, entry_evidence)
            evidence["fresh_price_edge"] = fresh_edge
        evidence_budget = evidence["ceiling_dollars"] if self.enforce_entry_risk else None
        qty, sizing = self._size(
            intent, net, equity, open_premium_at_risk=open_premium_at_risk,
            realised_loss=realised_loss, account=account,
            evidence_budget=evidence_budget, quantity_ceiling=quantity_ceiling)
        if self.enforce_entry_risk:
            sizing["fresh_price_edge"] = evidence.get("fresh_price_edge")

        scenario = None
        if priceable and self.enforce_entry_risk:
            spots = {str(key).upper(): float(value)
                     for key, value in (market_spots or {}).items() if float(value) > 0}
            candidate = {"underlying": intent.underlying, "legs": intent.legs}
            direction = (entry_evidence or {}).get("direction") or {}
            sigma = float(direction.get("sigma") or 0)
            sigmas = {intent.underlying: sigma} if sigma > 0 else None
            stress = portfolio_risk.stress_portfolio(
                open_positions, quotes, spots, now, candidate=candidate,
                sigma_by_underlying=sigmas,
                horizon_days=self.params.scenario_horizon_days,
                iv_shocks=(0.0, self.params.scenario_iv_shock_pct / 100.0))
            scenario = portfolio_risk.assess_admission(
                stress, equity, self.params.max_correlated_scenario_loss_pct, qty)
            binding = (stress or {}).get("worst_current") or {}
            binding_row = next((row for row in (stress or {}).get("scenarios") or []
                                if (row.get("spot_expected_move_multiple") ==
                                    binding.get("spot_expected_move_multiple")
                                    and row.get("iv_relative_shock") ==
                                    binding.get("iv_relative_shock"))), None)
            binding_contribution = float(
                (binding_row or {}).get("candidate_unit_pnl") or 0)
            scenario["candidate_unit_pnl_in_current_binding_scenario"] = round(
                binding_contribution, 2)
            scenario["measured_scenario_reducing"] = binding_contribution > 1e-9
            scenario_qty = int(scenario.get("allowed_qty") or 0)
            sizing["headroom_qty"]["portfolio_scenario"] = scenario_qty
            sizing["portfolio_scenario"] = scenario
            if scenario_qty < qty:
                qty = scenario_qty
                sizing["allowed_qty"] = qty
                sizing["binding_constraint"] = "portfolio_scenario"
        risk_reducing = bool(
            scenario and scenario.get("status") == "ok"
            and not scenario.get("resulting_breached")
            and (scenario.get("measured_scenario_reducing")
                 or (scenario.get("current_breached")
                     and float(scenario.get("resulting_worst_pnl") or 0)
                     > float(scenario.get("current_worst_pnl") or 0))))
        max_loss = st.max_loss(intent.legs, net, qty) if priceable else float("inf")
        max_profit = st.max_profit(intent.legs, net, qty) if priceable else 0.0

        if priceable:
            if self.enforce_entry_risk:
                results.append(GateResult(
                    "volatility_evidence", evidence["tier"] != "insufficient",
                    f"{evidence['tier']} tier: {evidence['positive_measure_count']}/"
                    f"{evidence['measure_count']} positive, median "
                    f"{evidence['edge_median']:.4f}, stable_top="
                    f"{evidence['stable_top']}; ceiling "
                    f"${evidence['ceiling_dollars']:,.0f}; scheduled-event "
                    f"multiplier {evidence['scheduled_event_multiplier']:.2f}"))
                results.append(edge_gate or GateResult(
                    "fresh_price_edge", False,
                    "fresh executable edge could not be computed"))
                scenario_ok = bool(scenario and scenario.get("status") == "ok"
                                   and scenario.get("allowed_qty", 0) >= 1
                                   and not scenario.get("resulting_breached"))
                results.append(GateResult(
                    "portfolio_scenario", scenario_ok,
                    (f"qty {qty}, resulting worst "
                     f"${float((scenario or {}).get('resulting_worst_pnl') or 0):,.0f} "
                     f"against ${float((scenario or {}).get('loss_limit_dollars') or 0):,.0f}; "
                     f"current breached={bool((scenario or {}).get('current_breached'))}"
                     if scenario and scenario.get("status") == "ok" else
                     f"incomplete stress; missing {(scenario or {}).get('missing_symbols', [])}")))
            results.append(gates.g_economics(intent.legs, net, qty, self.params))
            results.append(gates.g_risk_budget(max_loss, equity, open_premium_at_risk,
                                               realised_loss, self.params))
            results.append(gates.g_concentration(intent.underlying, open_positions or [],
                                                 self.params, family=intent.family,
                                                 net_price=net,
                                                 risk_reducing=risk_reducing))
            results.append(gates.g_buying_power(max_loss, account))

        condition_gate = _economic_condition_gate(
            net, economic_condition, priceable=priceable, now=now,
            deadline=authorization_deadline)
        if condition_gate is not None:
            results.append(condition_gate)

        verified = VerifiedTradeIntent(
            intent=intent, qty=qty, limit_price=round(net, 2),
            max_loss=max_loss, max_profit=max_profit,
            quote_snapshot_hash=_quote_hash(quotes),
            materialised_at=now, ttl_seconds=TTL_SECONDS)
        staged = StagedOrder(
            verified, results, self.program_id, sizing=sizing,
            economic_condition=economic_condition,
            authorization_deadline=authorization_deadline)
        if store:
            self._staged[self._stage_key(intent, economic_condition)] = staged
        return staged

    def _size(self, intent: TradeIntent, net_price: float, equity: float, *,
              open_premium_at_risk: float = 0.0, realised_loss: float = 0.0,
              account: dict | None = None,
              evidence_budget: float | None = None,
              quantity_ceiling: int | None = None) -> tuple[int, dict]:
        """Choose the smallest host-computed risk headroom and name the binding one."""
        if net_price == 0:
            return 0, {"requested_qty": 0, "allowed_qty": 0,
                       "binding_constraint": "unpriceable", "headroom_qty": {}}
        per_unit = st.max_loss(intent.legs, net_price, 1)
        if per_unit <= 0 or not math.isfinite(per_unit):
            return 0, {"requested_qty": 0, "allowed_qty": 0,
                       "binding_constraint": "unbounded_or_invalid_loss",
                       "headroom_qty": {}}
        total_cap = equity * self.params.max_total_premium_at_risk_pct / 100
        total_remaining = max(total_cap - open_premium_at_risk, 0.0)
        buying_power = float((account or {}).get(
            "options_buying_power", (account or {}).get("buying_power", 0)) or 0)
        throttle = equity * self.params.realised_loss_throttle_pct / 100
        headroom = {
            "requested_budget": max(int(intent.risk_budget // per_unit), 0),
            "single_position": max(int(
                (equity * self.params.max_single_position_pct / 100) // per_unit), 0),
            "portfolio": (0 if not math.isfinite(total_remaining)
                          else max(int(total_remaining // per_unit), 0)),
            "buying_power": max(int(buying_power // per_unit), 0),
        }
        if realised_loss >= throttle:
            headroom["realised_loss_throttle"] = 0
        if evidence_budget is not None:
            headroom["volatility_evidence"] = max(int(evidence_budget // per_unit), 0)
        if quantity_ceiling is not None:
            headroom["confirmation_ceiling"] = max(int(quantity_ceiling), 0)
        binding, allowed = min(headroom.items(), key=lambda item: (item[1], item[0]))
        return allowed, {
            "requested_qty": headroom["requested_budget"],
            "allowed_qty": allowed, "binding_constraint": binding,
            "headroom_qty": headroom, "per_unit_max_loss": per_unit,
        }

    @staticmethod
    def _key(intent: TradeIntent) -> str:
        canonical = {
            "underlying": str(intent.underlying).upper(), "family": str(intent.family),
            "thesis_id": str(intent.thesis_id), "risk_budget": float(intent.risk_budget),
            "note": str(intent.note),
            "legs": sorted(
                [{"symbol": str(l.symbol).upper(), "ratio_qty": int(l.ratio_qty),
                  "side": str(l.side), "position_intent": str(l.position_intent),
                  "strike": float(l.strike), "option_type": str(l.option_type).lower(),
                  "expiry": l.expiry.isoformat()} for l in intent.legs],
                key=lambda leg: (leg["symbol"], leg["side"], leg["position_intent"]))}
        blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    @classmethod
    def _stage_key(cls, intent: TradeIntent,
                   economic_condition: dict | None = None) -> str:
        if not economic_condition:
            return cls._key(intent)
        canonical = {
            "intent": cls._key(intent),
            "economic_condition": economic_condition or None,
        }
        blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    @staticmethod
    def _legs_json(intent: TradeIntent) -> list[dict]:
        return [{"symbol": l.symbol, "ratio_qty": l.ratio_qty, "side": l.side,
                 "position_intent": l.position_intent, "strike": l.strike,
                 "option_type": l.option_type, "expiry": l.expiry.isoformat()}
                for l in intent.legs]

    @staticmethod
    def _broker_request(legs: list[dict], qty: int, limit_price: float,
                        client_order_id: str, tif: str = "day") -> dict:
        if len(legs) == 1:
            leg = legs[0]
            return {"symbol": leg["symbol"], "qty": str(qty), "side": leg["side"],
                    "type": "limit", "limit_price": f"{limit_price:.2f}",
                    "time_in_force": tif, "position_intent": leg["position_intent"],
                    "client_order_id": client_order_id}
        return {"order_class": "mleg", "qty": str(qty), "type": "limit",
                "limit_price": f"{limit_price:.2f}", "time_in_force": tif,
                "client_order_id": client_order_id, "legs": legs}

    def _submit_body(self, request: dict) -> dict:
        """Use the exact persisted body on both first submission and safe retry."""
        if hasattr(self.rest, "submit_order_body"):
            return self.rest.submit_order_body(request)
        if request.get("order_class") == "mleg":
            return self.rest.submit_mleg(
                request["legs"], int(request["qty"]), float(request["limit_price"]),
                request["client_order_id"], request.get("time_in_force", "day"))
        return self.rest.submit_single(
            request["symbol"], int(request["qty"]), request["side"],
            request["position_intent"], float(request["limit_price"]),
            request["client_order_id"], request.get("time_in_force", "day"))

    @staticmethod
    def _http_status(exc: Exception) -> int | None:
        direct = getattr(exc, "status_code", None)
        if direct is not None:
            return int(direct)
        response = getattr(exc, "response", None)
        return int(response.status_code) if response is not None else None

    @staticmethod
    def _duplicate_client_id(exc: Exception) -> bool:
        if Executor._http_status(exc) != 422:
            return False
        response = getattr(exc, "response", None)
        payload = getattr(exc, "payload", None)
        text = " ".join((str(getattr(response, "text", "") or ""),
                         str(payload or ""), str(exc))).lower()
        return "client_order_id" in text and "unique" in text

    def _bind_prepared(self, prepared: dict, order: dict, *, strict: bool) -> dict:
        if self.ledger is None:
            return order
        coid = str(prepared["client_order_id"])
        if strict:
            observed = canonical_broker_order(order)
            observed_hash = self.ledger.request_fingerprint(observed)
            if observed_hash != prepared.get("request_fingerprint"):
                self.ledger.record_execution_state(
                    coid, "mismatch", observed_request=observed,
                    observed_fingerprint=observed_hash,
                    error="broker order does not match the durable request")
                return {"status": "mismatch", "client_order_id": coid,
                        "reason": "broker order does not match durable request"}
        oid = str(order.get("id") or "")
        if not oid:
            self.ledger.record_execution_state(
                coid, "unknown", error="broker response has no order id")
            return {"status": "unknown", "client_order_id": coid}
        if self.ledger.descriptor_by_client_id(coid) is None:
            self.ledger.record_order(
                order_id=oid, client_order_id=coid,
                structure_id=str(prepared["structure_id"]),
                purpose=str(prepared["purpose"]), thesis_id=str(prepared["thesis_id"]),
                underlying=str(prepared["underlying"]), family=str(prepared["family"]),
                legs=list(prepared["legs"]), qty=int(prepared["qty"]),
                signed_limit_price=float(prepared["signed_limit_price"]),
                max_loss_per_unit=float(prepared["max_loss_per_unit"]),
                cycle_id=prepared.get("cycle_id"), reason=str(prepared.get("reason") or ""),
                must_fill=bool(prepared.get("must_fill")),
                exit_intent_id=str(prepared.get("exit_intent_id") or ""),
                status=str(order.get("status") or "new"),
                filled_qty=float(order.get("filled_qty") or 0),
                filled_avg_price=(float(order["filled_avg_price"])
                                  if order.get("filled_avg_price") is not None else None))
        else:
            self.ledger.record_state(order)
        self.ledger.record_execution_state(
            coid, "submitted", order_id=oid, lookup_attempts=0,
            consecutive_404=0, last_checked_at=dt.datetime.now(dt.timezone.utc).isoformat())
        return order

    def _durable_submit(self, *, request: dict, descriptor: dict) -> dict:
        """Fsync intent, submit once, and preserve ambiguity as durable state."""
        with self._submit_lock:
            return self._durable_submit_locked(request=request, descriptor=descriptor)

    def _durable_submit_locked(self, *, request: dict, descriptor: dict) -> dict:
        if self.ledger is None:
            return self._submit_body(request)
        normalized = canonical_order_request(request)
        prepared = self.ledger.prepare_submission(
            client_order_id=request["client_order_id"], request=request,
            request_fingerprint=self.ledger.request_fingerprint(normalized), **descriptor)
        try:
            order = self._submit_body(request)
        except Exception as exc:
            status = self._http_status(exc)
            if self._duplicate_client_id(exc):
                self.ledger.record_execution_state(
                    request["client_order_id"], "unknown", error=str(exc),
                    duplicate_client_order_id=True)
                return {"status": "unknown", "client_order_id": request["client_order_id"],
                        "reason": "duplicate client order id; reconciliation required"}
            if status is not None and 400 <= status < 500 and status != 408:
                self.ledger.record_execution_state(
                    request["client_order_id"], "rejected", error=str(exc),
                    http_status=status)
                return {"status": "rejected", "client_order_id": request["client_order_id"],
                        "reason": str(exc)}
            self.ledger.record_execution_state(
                request["client_order_id"], "unknown", error=str(exc),
                http_status=status)
            return {"status": "unknown", "client_order_id": request["client_order_id"],
                    "reason": "submission response was ambiguous; reconciliation pending"}
        return self._bind_prepared(prepared, order, strict=False)

    def retry_not_found(self, client_order_id: str) -> dict:
        """Retry one confirmed-absent request with the exact same ID and body."""
        if self.ledger is None:
            raise RuntimeError("durable retry requires a ledger")
        prepared = self.ledger.execution(client_order_id)
        if prepared is None or prepared.get("status") != "not_found":
            raise ValueError(f"execution {client_order_id!r} is not confirmed absent")
        resubmits = int(prepared.get("resubmits") or 0)
        if resubmits >= 1:
            return {"status": "not_found", "client_order_id": client_order_id,
                    "reason": "exact retry already attempted"}
        self.ledger.record_execution_state(
            client_order_id, "pre_submit", resubmits=resubmits + 1,
            lookup_attempts=0, consecutive_404=0)
        try:
            order = self._submit_body(dict(prepared["request"]))
        except Exception as exc:
            if self._duplicate_client_id(exc):
                self.ledger.record_execution_state(
                    client_order_id, "unknown", error=str(exc),
                    duplicate_client_order_id=True, resubmits=resubmits + 1)
                return {"status": "unknown", "client_order_id": client_order_id}
            status = self._http_status(exc)
            if status is not None and 400 <= status < 500 and status != 408:
                self.ledger.record_execution_state(
                    client_order_id, "rejected", error=str(exc), http_status=status,
                    resubmits=resubmits + 1)
                return {"status": "rejected", "client_order_id": client_order_id}
            self.ledger.record_execution_state(
                client_order_id, "unknown", error=str(exc), http_status=status,
                resubmits=resubmits + 1)
            return {"status": "unknown", "client_order_id": client_order_id}
        return self._bind_prepared(prepared, order, strict=False)

    def reconcile_unresolved(self, *, now: dt.datetime | None = None) -> list[dict]:
        """Resolve only due PRE_SUBMIT/UNKNOWN records, exits first, with backoff."""
        if self.ledger is None or not hasattr(self.rest, "order_by_client_order_id"):
            return []
        now = now or dt.datetime.now(dt.timezone.utc)
        out = []
        for prepared in self.ledger.unresolved_executions(now=now, due_only=True):
            coid = str(prepared["client_order_id"])
            try:
                order = self.rest.order_by_client_order_id(coid)
            except Exception as exc:
                if self._http_status(exc) == 404:
                    state = self.ledger.mark_lookup_404(coid, now=now)
                    result = {"client_order_id": coid, "status": state["status"]}
                    if state["status"] == "not_found":
                        created = dt.datetime.fromisoformat(str(prepared["created_at"]))
                        if ((now - created).total_seconds() <= EXACT_RETRY_MAX_AGE_SECONDS
                                and int(prepared.get("resubmits") or 0) < 1):
                            result = self.retry_not_found(coid)
                    out.append(result)
                else:
                    self.ledger.mark_lookup_error(coid, str(exc), now=now)
                    out.append({"client_order_id": coid, "status": "unknown"})
            else:
                bound = self._bind_prepared(prepared, order, strict=True)
                out.append({"client_order_id": coid,
                            "order_id": bound.get("id"),
                            "status": bound.get("status")})
        return out

    def entry_blockers(self) -> list[dict]:
        return self.ledger.entry_blockers() if self.ledger is not None else []

    def scan_prefixed_open_orders(self) -> list[dict]:
        """Startup catch-all for orders created by an older, post-submit-only build."""
        if self.ledger is None or not hasattr(self.rest, "orders"):
            return []
        alerts = []
        for order in self.rest.orders("open"):
            coid = str(order.get("client_order_id") or "")
            if not coid.startswith(("a", "x")):
                continue
            if (self.ledger.execution(coid) is not None
                    or self.ledger.descriptor_by_client_id(coid) is not None
                    or coid in self.ledger.execution_alerts()):
                continue
            alerts.append(self.ledger.record_execution_alert(
                coid, order_id=str(order.get("id") or ""),
                reason="open order has our prefix but no durable local intent"))
        return alerts

    def execute(self, intent: TradeIntent, *, economic_condition: dict | None = None,
                authorization_seconds: float | None = None,
                **materialise_kwargs) -> dict:
        """Stage first; confirm only from a later model program in this cycle.

        If an economic condition is supplied it is part of the draft identity and
        is rechecked from fresh executable quotes immediately before submission.
        The time window begins at staging and can never be extended by a slow
        confirmation turn.
        """
        canonical = resolve_intent(self.rest, intent)
        condition = dict(economic_condition or {}) or None
        key = self._stage_key(canonical, condition)
        if key not in self._staged:
            seconds = float(authorization_seconds or 30.0) if condition else None
            if condition and not 5 <= seconds <= 120:
                raise ValueError("authorization_seconds must be between 5 and 120")
            stage_now = materialise_kwargs.get("now") or dt.datetime.now(dt.timezone.utc)
            deadline = (stage_now + dt.timedelta(seconds=seconds)
                        if condition else None)
            self._staged.clear()  # one draft per cycle; a changed intent replaces it
            staged = self.materialise(
                canonical, economic_condition=condition,
                authorization_deadline=deadline, **materialise_kwargs)
            maximum_profit = (None if staged.verified.max_profit == st.UNBOUNDED
                              else staged.verified.max_profit)
            if not staged.economic_condition_passed:
                self._staged.pop(key, None)
                return {"status": "condition_not_met", "qty": staged.verified.qty,
                        "limit_price": staged.verified.limit_price,
                        "condition": condition, "checklist": staged.checklist(),
                        "next": "do not chase; reconsider or arm a short-lived trigger"}
            return {"status": "staged", "qty": staged.verified.qty,
                    "limit_price": staged.verified.limit_price,
                    "max_loss": staged.verified.max_loss,
                    "max_profit": maximum_profit,
                    "sizing": staged.sizing,
                    "passed": staged.passed,
                    "checklist": staged.checklist(),
                    "confirmation_call": staged.confirmation_call(),
                    "next": (
                        "inspect the checklist; a later model program must repeat "
                        "trading.execute_if with the identical intent, price boundary, "
                        "and valid_for_seconds shown in confirmation_call"
                        if condition else
                        "inspect the checklist; a later model program may call "
                        "trading.execute with the identical intent to confirm")}
        staged = self._staged[key]
        if (self.program_id is not None
                and staged.staged_program_id == self.program_id):
            maximum_profit = (None if staged.verified.max_profit == st.UNBOUNDED
                              else staged.verified.max_profit)
            return {"status": "awaiting_confirmation", "qty": staged.verified.qty,
                    "limit_price": staged.verified.limit_price,
                    "max_loss": staged.verified.max_loss,
                    "max_profit": maximum_profit,
                    "sizing": staged.sizing,
                    "passed": staged.passed,
                    "checklist": staged.checklist(),
                    "confirmation_call": staged.confirmation_call(),
                    "next": (
                        "confirmation is accepted only from the next model program, "
                        "using the exact confirmation_call"
                    )}
        return self.confirm(
            canonical, economic_condition=condition,
            authorization_deadline=staged.authorization_deadline,
            **materialise_kwargs)

    # ---- confirmation ------------------------------------------------------
    def confirm(self, intent: TradeIntent, *, now: dt.datetime | None = None,
                economic_condition: dict | None = None,
                authorization_deadline: dt.datetime | None = None,
                **materialise_kwargs) -> dict:
        """Second call with an identical intent, rechecked against fresh state."""
        now = now or dt.datetime.now(dt.timezone.utc)
        intent = resolve_intent(self.rest, intent)
        condition = dict(economic_condition or {}) or None
        key = self._stage_key(intent, condition)
        intent_key = self._key(intent)
        if intent_key in self._terminal_intents:
            raise PermissionError("this intent was already executed")
        staged = self._staged.get(key)
        if staged is None:
            raise PermissionError("nothing staged for this intent -- call execute() first")
        if staged.verified.nonce in self._consumed:
            raise PermissionError("this intent was already executed")
        if condition and staged.authorization_deadline is not None \
                and now > staged.authorization_deadline:
            self._staged.pop(key, None)
            return {"status": "condition_expired",
                    "reason": "price authorization expired before confirmation",
                    "condition": condition}
        if staged.verified.expired(now):
            if not condition:
                staged = self.materialise(intent, now=now, **materialise_kwargs)
                return {"status": "restaged",
                        "reason": "TTL lapsed, repriced from fresh quotes",
                        "checklist": staged.checklist()}
            # Quote-review TTL and economic authorization answer different
            # questions.  A slow model turn may outlive the former while the
            # explicit price boundary is still valid.  Continue below and
            # re-materialize from fresh quotes, account state and book risk inside
            # the original (never extended) authorization window.
        original_nonce = staged.verified.nonce
        # Confirmation authorizes the reviewed geometry, not stale quotes or stale
        # book risk. Re-materialize every time and never increase above the quantity
        # shown in the staged checklist. A deterioration can reduce or block it.
        staged = self.materialise(
            intent, now=now, store=False,
            quantity_ceiling=staged.verified.qty,
            economic_condition=condition,
            authorization_deadline=(authorization_deadline or
                                    staged.authorization_deadline),
            **materialise_kwargs)
        self._staged[key] = staged
        if not staged.economic_condition_passed:
            self._staged.pop(key, None)
            return {"status": "condition_not_met", "qty": staged.verified.qty,
                    "limit_price": staged.verified.limit_price,
                    "condition": condition, "checklist": staged.checklist(),
                    "next": "do not chase; reconsider or arm a short-lived trigger"}
        if not staged.passed:
            return {"status": "blocked", "outcome": "BLOCKED_RISK",
                    "sizing": staged.sizing, "checklist": staged.checklist()}
        if staged.verified.qty < 1:
            return {"status": "blocked", "outcome": "BLOCKED_RISK",
                    "sizing": staged.sizing,
                    "checklist": "qty resolved to 0 under host-computed headroom"}
        if self.mode != "execute":
            return {"status": "proposed", "checklist": staged.checklist(),
                    "note": "propose mode -- freshly revalidated; no order submitted"}

        v = staged.verified
        blockers = self.entry_blockers()
        if blockers:
            return {"status": "blocked", "outcome": "BLOCKED_RISK",
                    "reason": "new entries frozen while execution reconciliation is unresolved",
                    "blockers": [{"client_order_id": b.get("client_order_id"),
                                  "status": b.get("status")} for b in blockers]}
        legs = [{"symbol": l.symbol, "ratio_qty": str(l.ratio_qty), "side": l.side,
                 "position_intent": l.position_intent} for l in v.intent.legs]
        coid = v.client_order_id()
        request = self._broker_request(legs, v.qty, v.limit_price, coid)
        order = self._durable_submit(
            request=request,
            descriptor={"structure_id": self._key(v.intent), "purpose": "entry",
                        "thesis_id": v.intent.thesis_id,
                        "underlying": v.intent.underlying, "family": v.intent.family,
                        "legs": self._legs_json(v.intent), "qty": v.qty,
                        "signed_limit_price": v.limit_price,
                        "max_loss_per_unit": v.max_loss / v.qty if v.qty else 0.0,
                        "cycle_id": self.cycle_id})
        raw_status = str(order.get("status") or "submitted")
        attempt = raw_status if raw_status in ("unknown", "rejected", "mismatch") \
            else "submitted"
        self._consumed.add(v.nonce)
        self._consumed.add(original_nonce)
        self._attempt_status[v.nonce] = attempt
        # A terminal broker outcome (including UNKNOWN, which is now ledger-owned)
        # ends the draft. Reconciliation continues from the durable submission.
        self._staged.pop(key, None)
        self._terminal_intents.add(intent_key)
        if attempt in ("unknown", "rejected", "mismatch"):
            return {**order, "qty": v.qty, "limit_price": v.limit_price,
                    "max_loss": v.max_loss, "checklist": staged.checklist()}
        return {"status": "submitted", "order_id": order.get("id"),
                "client_order_id": coid, "qty": v.qty, "limit_price": v.limit_price,
                "max_loss": v.max_loss, "checklist": staged.checklist()}

    def execute_authorized(self, intent: TradeIntent, *, trigger_id: str,
                           economic_condition: dict,
                           authorization_deadline: dt.datetime,
                           now: dt.datetime | None = None,
                           **materialise_kwargs) -> dict:
        """Execute a previously authorized one-shot trigger from fresh state.

        Arming the trigger is the deliberation boundary.  This path therefore has
        no second model confirmation, but it runs the same materialisation and
        host gates and uses a deterministic client order ID for crash-safe retry.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        intent = resolve_intent(self.rest, intent)
        staged = self.materialise(
            intent, now=now, store=False,
            economic_condition=economic_condition,
            authorization_deadline=authorization_deadline,
            **materialise_kwargs)
        if not staged.economic_condition_passed:
            return {"status": "condition_not_met", "qty": staged.verified.qty,
                    "limit_price": staged.verified.limit_price,
                    "condition": economic_condition,
                    "checklist": staged.checklist()}
        if not staged.passed or staged.verified.qty < 1:
            failed = [row for row in staged.results if not row.passed]
            return {"status": "blocked", "outcome": "BLOCKED_RISK",
                    "reason": "one or more host admission gates refused the trigger",
                    "failed_gates": [row.name for row in failed]
                                    or ["quantity_headroom"],
                    "failed_gate_details": [
                        {"name": row.name, "reason": row.reason}
                        for row in failed] or [{
                            "name": "quantity_headroom",
                            "reason": "host-computed permitted quantity is zero"}],
                    "sizing": staged.sizing, "checklist": staged.checklist()}
        if self.mode != "execute":
            return {"status": "proposed", "checklist": staged.checklist(),
                    "note": "propose mode -- trigger would submit now"}
        blockers = self.entry_blockers()
        if blockers:
            return {"status": "blocked", "outcome": "BLOCKED_RISK",
                    "reason": "new entries frozen while execution reconciliation is unresolved",
                    "failed_gates": ["execution_reconciliation"],
                    "failed_gate_details": [{
                        "name": "execution_reconciliation",
                        "reason": "one or more durable submissions remain unresolved"}],
                    "blockers": [{"client_order_id": row.get("client_order_id"),
                                  "status": row.get("status")} for row in blockers]}

        v = staged.verified
        coid = "a" + hashlib.sha256(
            f"trigger/{trigger_id}/{self._key(intent)}/{v.qty}".encode()
        ).hexdigest()[:31]
        if self.ledger is not None:
            prior = self.ledger.execution(coid)
            if prior is not None:
                return {"status": "already_pending",
                        "client_order_id": coid,
                        "order_id": prior.get("order_id"),
                        "broker_status": prior.get("status")}
        legs = [{"symbol": leg.symbol, "ratio_qty": str(leg.ratio_qty),
                 "side": leg.side, "position_intent": leg.position_intent}
                for leg in v.intent.legs]
        request = self._broker_request(legs, v.qty, v.limit_price, coid)
        order = self._durable_submit(
            request=request,
            descriptor={"structure_id": self._key(v.intent), "purpose": "entry",
                        "thesis_id": v.intent.thesis_id,
                        "underlying": v.intent.underlying, "family": v.intent.family,
                        "legs": self._legs_json(v.intent), "qty": v.qty,
                        "signed_limit_price": v.limit_price,
                        "max_loss_per_unit": v.max_loss / v.qty if v.qty else 0.0,
                        "cycle_id": self.cycle_id,
                        "reason": f"action trigger {trigger_id}"})
        raw_status = str(order.get("status") or "submitted")
        if raw_status in ("unknown", "rejected", "mismatch"):
            return {**order, "qty": v.qty, "limit_price": v.limit_price,
                    "max_loss": v.max_loss, "checklist": staged.checklist()}
        return {"status": "submitted", "order_id": order.get("id"),
                "client_order_id": coid, "qty": v.qty,
                "limit_price": v.limit_price, "max_loss": v.max_loss,
                "checklist": staged.checklist()}

    # ---- closing and reconciliation ---------------------------------------
    def close_structure(self, structure: dict, *, reason: str,
                        now: dt.datetime | None = None,
                        min_executable_profit: float | None = None,
                        client_order_seed: str | None = None,
                        must_fill: bool = False,
                        mandatory_source: str = "") -> dict:
        """Submit a closing order for one normalized structure.

        Existing active exits are returned instead of duplicated, which remains
        true after a process restart because the check is ledger-backed.
        """
        sid = str(structure["structure_id"])
        exit_intent = None
        if must_fill and self.ledger is not None:
            exit_intent = self.ledger.arm_exit_intent(
                structure_id=sid,
                thesis_id=str(structure.get("thesis_id") or ""),
                reason=reason, source=mandatory_source or "host_mandatory_exit")
        if self.ledger is not None:
            pending = self.ledger.active_exit(sid)
            if pending:
                return {"status": "already_pending", "order_id": pending.get("order_id"),
                        "client_order_id": pending.get("client_order_id"),
                        "structure_id": sid}
        qty = int(structure.get("qty") or 0)
        if qty < 1:
            return {"status": "flat", "structure_id": sid}
        account = self.rest.account()
        identity = gates.g_account_identity(
            account, self.profile_name, self.expected_account_id or "", now=now)
        if not identity.passed:
            raise PermissionError(identity.reason)

        closing = []
        for raw in structure["legs"]:
            was_long = raw["side"] == "buy"
            closing.append({**raw, "side": "sell" if was_long else "buy",
                            "position_intent": "sell_to_close" if was_long
                            else "buy_to_close"})
        # Buy back shorts before selling longs in the submitted leg list. Alpaca
        # accepts the structure as one order, but this also gives downstream tools
        # the safest deterministic ordering if they ever inspect or replay its legs.
        closing.sort(key=lambda leg: leg["position_intent"] != "buy_to_close")
        quotes = self.rest.option_quotes([l["symbol"] for l in closing])
        if any(l["symbol"] not in quotes for l in closing):
            missing = [l["symbol"] for l in closing if l["symbol"] not in quotes]
            raise RuntimeError(f"cannot price closing order; missing quotes: {missing}")
        net = sum((1 if l["side"] == "buy" else -1) * int(l["ratio_qty"])
                  * _leg_price(quotes[l["symbol"]], l["side"]) for l in closing)
        if abs(net) < 0.01:
            net = 0.01 if any(l["side"] == "buy" for l in closing) else -0.01
        limit_price = round(net, 2)
        entry_price = float(structure.get("cost_basis") or 0) \
            / (qty * CONTRACT_MULTIPLIER)
        liquidation = -limit_price
        executable_profit = round(
            (liquidation - entry_price) * qty * CONTRACT_MULTIPLIER, 2)
        if min_executable_profit is not None:
            threshold = float(min_executable_profit)
            if not math.isfinite(threshold):
                raise ValueError("min_executable_profit must be finite")
            if executable_profit + 1e-9 < threshold:
                return {"status": "condition_not_met", "structure_id": sid,
                        "executable_profit": executable_profit,
                        "min_executable_profit": round(threshold, 2),
                        "limit_price": limit_price,
                        "reason": "fresh executable profit is below the authorized floor"}
        nonce = client_order_seed or uuid.uuid4().hex
        coid = "x" + hashlib.sha256(
            f"{sid}/{reason}/{self.cycle_id}/{nonce}".encode()).hexdigest()[:31]
        if client_order_seed and self.ledger is not None:
            prior = self.ledger.execution(coid)
            if prior is not None:
                return {"status": "already_pending", "order_id": prior.get("order_id"),
                        "client_order_id": coid, "structure_id": sid,
                        "executable_profit": executable_profit}
        api_legs = [{"symbol": l["symbol"], "ratio_qty": str(l["ratio_qty"]),
                     "side": l["side"], "position_intent": l["position_intent"]}
                    for l in closing]
        if self.mode != "execute":
            return {"status": "proposed_close", "structure_id": sid, "qty": qty,
                    "limit_price": limit_price, "reason": reason, "legs": api_legs,
                    "executable_profit": executable_profit}
        request = self._broker_request(api_legs, qty, limit_price, coid)
        try:
            order = self._durable_submit(
                request=request,
                descriptor={"structure_id": sid, "purpose": "exit",
                        "thesis_id": str(structure.get("thesis_id") or ""),
                        "underlying": str(structure["underlying"]),
                        "family": str(structure["family"]),
                        # The ledger must describe what was sent, not the opening legs.
                        "legs": list(closing), "qty": qty,
                        "signed_limit_price": limit_price,
                        "max_loss_per_unit": float(
                            structure.get("max_loss_per_unit") or 0),
                        "cycle_id": self.cycle_id, "reason": reason,
                        "must_fill": bool(must_fill),
                        "exit_intent_id": str(
                            (exit_intent or {}).get("exit_intent_id") or "")})
        except ValueError as exc:
            if "already active" not in str(exc):
                raise
            pending = self.ledger.active_exit(sid) if self.ledger is not None else None
            return {"status": "already_pending", "order_id": (pending or {}).get("order_id"),
                    "client_order_id": (pending or {}).get("client_order_id"),
                    "structure_id": sid}
        status = str(order.get("status") or "submitted")
        if status in ("unknown", "rejected", "mismatch"):
            return {**order, "structure_id": sid, "qty": qty,
                    "limit_price": limit_price, "reason": reason,
                    "executable_profit": executable_profit}
        result = {"status": "submitted_close", "order_id": order.get("id"),
                "client_order_id": coid, "structure_id": sid, "qty": qty,
                "limit_price": limit_price, "reason": reason,
                "executable_profit": executable_profit,
                "must_fill": bool(must_fill)}
        if exit_intent is not None and self.ledger is not None:
            current = self.ledger.exit_intents().get(sid) or exit_intent
            self.ledger.record_exit_intent_state(
                sid, "active", attempts=int(current.get("attempts") or 0) + 1,
                last_order_id=str(order.get("id") or ""),
                last_client_order_id=coid,
                last_submitted_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                last_limit_price=limit_price)
        return result

    def reconcile_order(self, order_id: str) -> dict:
        order = self.rest.order(order_id)
        state = self.ledger.record_state(order) if self.ledger is not None else order
        return {"order_id": order_id, "status": order.get("status"),
                "filled_qty": float(order.get("filled_qty") or 0),
                "filled_avg_price": order.get("filled_avg_price"),
                "delta_filled_qty": float(state.get("delta_filled_qty") or 0)}

    def reconcile_orders(self, *, cancel_after_s: float | None = None,
                         now: dt.datetime | None = None) -> list[dict]:
        if self.ledger is None:
            return []
        now = now or dt.datetime.now(dt.timezone.utc)
        out = self.reconcile_unresolved(now=now)
        descs = self.ledger.descriptors()
        for oid in self.ledger.pending_order_ids():
            state = self.reconcile_order(oid)
            out.append(state)
            if (cancel_after_s is not None
                    and str(state.get("status", "")).lower() not in TERMINAL_STATUSES):
                submitted = dt.datetime.fromisoformat(descs[oid]["ts"])
                if (now - submitted).total_seconds() >= cancel_after_s:
                    self.rest.cancel(oid)
                    self.ledger.record_state({"id": oid, "status": "canceled",
                                              "filled_qty": state["filled_qty"],
                                              "filled_avg_price": state["filled_avg_price"]})
                    state["status"] = "canceled"
        return out

    # ---- fill management ---------------------------------------------------
    def manage_fill(self, order_id: str, *, steps: int = 3, step_seconds: float = 20.0,
                    sleep=time.sleep) -> dict:
        """Open at the computed limit, reprice toward the far side, cancel on timeout."""
        for attempt in range(steps):
            sleep(step_seconds)
            o = self.rest.order(order_id)
            if self.ledger is not None:
                self.ledger.record_state(o)
            status = o.get("status")
            if status == "filled":
                return {"status": "filled", "price": o.get("filled_avg_price"),
                        "attempts": attempt + 1}
            if status in ("canceled", "expired", "rejected"):
                return {"status": status, "attempts": attempt + 1}
            if attempt == steps - 1:
                self.rest.cancel(order_id)
                if self.ledger is not None:
                    self.ledger.record_state({"id": order_id, "status": "canceled",
                                              "filled_qty": o.get("filled_qty", 0),
                                              "filled_avg_price": o.get("filled_avg_price")})
                return {"status": "cancelled_unfilled", "attempts": steps,
                        "partial": o.get("filled_qty")}
        return {"status": "unknown"}
