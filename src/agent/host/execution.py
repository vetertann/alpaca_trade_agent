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
import time
import uuid

from agent.host import gates
from agent.host.contracts import resolve_intent
from agent.host.ledger import ExecutionLedger, TERMINAL_STATUSES
from agent.host.rest import Rest
from agent.host.risk_params import RiskParams
from agent.quant import structures as st
from agent.types import (CONTRACT_MULTIPLIER, GateResult, Leg, TradeIntent,
                         VerifiedTradeIntent)

TTL_SECONDS = 45.0


class StagedOrder:
    def __init__(self, verified: VerifiedTradeIntent, results: list[GateResult],
                 staged_program_id: int | None = None):
        self.verified = verified
        self.results = results
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
        return head + "\n" + gates.render(self.results)


def _quote_hash(quotes: dict[str, dict]) -> str:
    blob = json.dumps({k: [v.get("bp"), v.get("ap")] for k, v in sorted(quotes.items())})
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _leg_price(quote: dict, side: str) -> float:
    """Conservative: buy at the ask, sell at the bid."""
    return float(quote["ap"] if side == "buy" else quote["bp"])


class Executor:
    def __init__(self, rest: Rest, params: RiskParams, profile_name: str,
                 mode: str = "propose", ledger: ExecutionLedger | None = None,
                 expected_account_id: str | None = None):
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
        self.cycle_id: str | None = None
        self.program_id: int | None = None

    def begin_cycle(self, cycle_id: str) -> None:
        """Staging is deliberately scoped to one model decision cycle."""
        self.cycle_id = cycle_id
        self.program_id = None
        self._staged.clear()

    def begin_program(self, program_id: int) -> None:
        """Mark the model-program boundary used by two-phase confirmation."""
        if self.cycle_id is None:
            raise RuntimeError("cannot begin a program outside a decision cycle")
        self.program_id = int(program_id)

    def end_cycle(self) -> None:
        self._staged.clear()
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
                    now: dt.datetime | None = None, store: bool = True) -> StagedOrder:
        intent = resolve_intent(self.rest, intent)
        symbols = [l.symbol for l in intent.legs]
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
        for sym in symbols:
            results.append(gates.g_quote_valid(sym, quotes.get(sym, {}), self.params, now=now))
            if quotes.get(sym):
                results.append(gates.g_spread(sym, quotes[sym], self.params))

        # Price from the quotes just fetched, never from anything the model supplied.
        net = 0.0
        priceable = all(quotes.get(s) for s in symbols)
        if priceable:
            for leg in intent.legs:
                net += leg.sign * leg.ratio_qty * _leg_price(quotes[leg.symbol], leg.side)

        qty = self._size(intent, net, equity)
        max_loss = st.max_loss(intent.legs, net, qty) if priceable else float("inf")
        max_profit = st.max_profit(intent.legs, net, qty) if priceable else 0.0

        if priceable:
            results.append(gates.g_economics(intent.legs, net, qty, self.params))
            results.append(gates.g_risk_budget(max_loss, equity, open_premium_at_risk,
                                               realised_loss, self.params))
            results.append(gates.g_concentration(intent.underlying, open_positions or [],
                                                 self.params))
            results.append(gates.g_buying_power(max_loss, account))

        verified = VerifiedTradeIntent(
            intent=intent, qty=qty, limit_price=round(net, 2),
            max_loss=max_loss, max_profit=max_profit,
            quote_snapshot_hash=_quote_hash(quotes),
            materialised_at=now, ttl_seconds=TTL_SECONDS)
        staged = StagedOrder(verified, results, self.program_id)
        if store:
            self._staged[self._key(intent)] = staged
        return staged

    def _size(self, intent: TradeIntent, net_price: float, equity: float) -> int:
        """Quantity from the risk budget, never from a model-supplied field."""
        if net_price == 0:
            return 0
        per_unit = st.max_loss(intent.legs, net_price, 1)
        if per_unit <= 0:
            return 0
        budget = min(intent.risk_budget, equity * self.params.max_single_position_pct / 100)
        return max(int(budget // per_unit), 0)

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

    @staticmethod
    def _legs_json(intent: TradeIntent) -> list[dict]:
        return [{"symbol": l.symbol, "ratio_qty": l.ratio_qty, "side": l.side,
                 "position_intent": l.position_intent, "strike": l.strike,
                 "option_type": l.option_type, "expiry": l.expiry.isoformat()}
                for l in intent.legs]

    def execute(self, intent: TradeIntent, **materialise_kwargs) -> dict:
        """Stage first; confirm only from a later model program in this cycle."""
        canonical = resolve_intent(self.rest, intent)
        key = self._key(canonical)
        if key not in self._staged:
            self._staged.clear()  # one draft per cycle; a changed intent replaces it
            staged = self.materialise(canonical, **materialise_kwargs)
            maximum_profit = (None if staged.verified.max_profit == st.UNBOUNDED
                              else staged.verified.max_profit)
            return {"status": "staged", "qty": staged.verified.qty,
                    "limit_price": staged.verified.limit_price,
                    "max_loss": staged.verified.max_loss,
                    "max_profit": maximum_profit,
                    "passed": staged.passed,
                    "checklist": staged.checklist(),
                    "next": "inspect the checklist; a later model program may call "
                            "trading.execute with the identical intent to confirm"}
        staged = self._staged[key]
        if (self.program_id is not None
                and staged.staged_program_id == self.program_id):
            maximum_profit = (None if staged.verified.max_profit == st.UNBOUNDED
                              else staged.verified.max_profit)
            return {"status": "awaiting_confirmation", "qty": staged.verified.qty,
                    "limit_price": staged.verified.limit_price,
                    "max_loss": staged.verified.max_loss,
                    "max_profit": maximum_profit,
                    "passed": staged.passed,
                    "checklist": staged.checklist(),
                    "next": "confirmation is accepted only from the next model program"}
        return self.confirm(canonical, **materialise_kwargs)

    # ---- confirmation ------------------------------------------------------
    def confirm(self, intent: TradeIntent, *, now: dt.datetime | None = None,
                **materialise_kwargs) -> dict:
        """Second call with an identical intent. Re-materialises if the TTL lapsed."""
        now = now or dt.datetime.now(dt.timezone.utc)
        intent = resolve_intent(self.rest, intent)
        key = self._key(intent)
        staged = self._staged.get(key)
        if staged is None:
            raise PermissionError("nothing staged for this intent -- call execute() first")
        if staged.verified.nonce in self._consumed:
            raise PermissionError("this intent was already executed")
        if staged.verified.expired(now):
            staged = self.materialise(intent, now=now, **materialise_kwargs)
            return {"status": "restaged", "reason": "TTL lapsed, repriced from fresh quotes",
                    "checklist": staged.checklist()}
        if not staged.passed:
            return {"status": "blocked", "checklist": staged.checklist()}
        if staged.verified.qty < 1:
            return {"status": "blocked", "checklist": "qty resolved to 0 under the risk budget"}
        if self.mode != "execute":
            return {"status": "proposed", "checklist": staged.checklist(),
                    "note": "propose mode -- no order submitted"}

        v = staged.verified
        legs = [{"symbol": l.symbol, "ratio_qty": str(l.ratio_qty), "side": l.side,
                 "position_intent": l.position_intent} for l in v.intent.legs]
        coid = v.client_order_id()
        if len(legs) == 1:
            order = self.rest.submit_single(legs[0]["symbol"], v.qty, legs[0]["side"],
                                            legs[0]["position_intent"], v.limit_price, coid)
        else:
            order = self.rest.submit_mleg(legs, v.qty, v.limit_price, coid)
        if self.ledger is not None:
            self.ledger.record_order(
                order_id=str(order["id"]), client_order_id=coid,
                structure_id=self._key(v.intent), purpose="entry",
                thesis_id=v.intent.thesis_id, underlying=v.intent.underlying,
                family=v.intent.family, legs=self._legs_json(v.intent), qty=v.qty,
                signed_limit_price=v.limit_price,
                max_loss_per_unit=v.max_loss / v.qty if v.qty else 0.0,
                cycle_id=self.cycle_id, status=str(order.get("status") or "new"),
                filled_qty=float(order.get("filled_qty") or 0),
                filled_avg_price=(float(order["filled_avg_price"])
                                  if order.get("filled_avg_price") is not None else None))
        self._consumed.add(v.nonce)
        return {"status": "submitted", "order_id": order.get("id"),
                "client_order_id": coid, "qty": v.qty, "limit_price": v.limit_price,
                "max_loss": v.max_loss, "checklist": staged.checklist()}

    # ---- closing and reconciliation ---------------------------------------
    def close_structure(self, structure: dict, *, reason: str,
                        now: dt.datetime | None = None) -> dict:
        """Submit a closing order for one normalized structure.

        Existing active exits are returned instead of duplicated, which remains
        true after a process restart because the check is ledger-backed.
        """
        sid = str(structure["structure_id"])
        if self.ledger is not None:
            pending = self.ledger.active_exit(sid)
            if pending:
                return {"status": "already_pending", "order_id": pending["order_id"],
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
        coid = "x" + hashlib.sha256(
            f"{sid}/{reason}/{self.cycle_id}/{uuid.uuid4().hex}".encode()).hexdigest()[:31]
        api_legs = [{"symbol": l["symbol"], "ratio_qty": str(l["ratio_qty"]),
                     "side": l["side"], "position_intent": l["position_intent"]}
                    for l in closing]
        if self.mode != "execute":
            return {"status": "proposed_close", "structure_id": sid, "qty": qty,
                    "limit_price": limit_price, "reason": reason, "legs": api_legs}
        if len(api_legs) == 1:
            l = api_legs[0]
            order = self.rest.submit_single(l["symbol"], qty, l["side"],
                                            l["position_intent"], limit_price, coid)
        else:
            order = self.rest.submit_mleg(api_legs, qty, limit_price, coid)
        if self.ledger is not None:
            self.ledger.record_order(
                order_id=str(order["id"]), client_order_id=coid, structure_id=sid,
                purpose="exit", thesis_id=str(structure.get("thesis_id") or ""),
                underlying=str(structure["underlying"]), family=str(structure["family"]),
                legs=list(structure["legs"]), qty=qty, signed_limit_price=limit_price,
                max_loss_per_unit=float(structure.get("max_loss_per_unit") or 0),
                cycle_id=self.cycle_id, reason=reason,
                status=str(order.get("status") or "new"),
                filled_qty=float(order.get("filled_qty") or 0),
                filled_avg_price=(float(order["filled_avg_price"])
                                  if order.get("filled_avg_price") is not None else None))
        return {"status": "submitted_close", "order_id": order.get("id"),
                "client_order_id": coid, "structure_id": sid, "qty": qty,
                "limit_price": limit_price, "reason": reason}

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
        descs = self.ledger.descriptors()
        out = []
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
