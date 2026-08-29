"""Append-only execution ledger and broker-position reconciliation.

The broker is authoritative for current positions and order state.  The ledger
preserves structure membership and entry/exit cash flows that the positions
endpoint does not expose, so the two are deliberately reconciled rather than one
being treated as a replacement for the other.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Any

from agent.host.contracts import parse_occ_symbol


TERMINAL_STATUSES = {"filled", "canceled", "cancelled", "expired", "rejected"}


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ExecutionLedger:
    def __init__(self, path: str | Path = ".run/execution.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _append(self, kind: str, **fields: Any) -> dict:
        rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
               "kind": kind, **fields}
        line = json.dumps(rec, default=str) + "\n"
        with self._lock, self.path.open("a") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return rec

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text().splitlines()
            out = []
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    if index == len(lines) - 1:
                        break  # tolerate one torn append after process/VM loss
                    raise
            return out

    def record_order(self, *, order_id: str, client_order_id: str,
                     structure_id: str, purpose: str, thesis_id: str,
                     underlying: str, family: str, legs: list[dict], qty: int,
                     signed_limit_price: float, max_loss_per_unit: float,
                     cycle_id: str | None, reason: str = "", status: str = "new",
                     filled_qty: float = 0.0, filled_avg_price: float | None = None) -> dict:
        return self._append(
            "ORDER", order_id=order_id, client_order_id=client_order_id,
            structure_id=structure_id, purpose=purpose, thesis_id=thesis_id,
            underlying=underlying, family=family, legs=legs, qty=int(qty),
            signed_limit_price=float(signed_limit_price),
            max_loss_per_unit=float(max_loss_per_unit), cycle_id=cycle_id,
            reason=reason, status=status, filled_qty=float(filled_qty),
            filled_avg_price=filled_avg_price)

    def descriptors(self) -> dict[str, dict]:
        return {r["order_id"]: r for r in self.records() if r.get("kind") == "ORDER"}

    def states(self) -> dict[str, dict]:
        states: dict[str, dict] = {}
        for r in self.records():
            if r.get("kind") == "ORDER":
                states[r["order_id"]] = {
                    "order_id": r["order_id"], "status": r.get("status", "new"),
                    "filled_qty": _f(r.get("filled_qty")),
                    "filled_avg_price": r.get("filled_avg_price"),
                    "updated_at": r["ts"]}
            elif r.get("kind") == "ORDER_STATE":
                states[r["order_id"]] = r
        return states

    def record_state(self, order: dict) -> dict:
        oid = str(order.get("id") or order.get("order_id") or "")
        if not oid:
            raise ValueError("broker order has no id")
        with self._lock:
            previous = self.states().get(oid, {})
            old_qty = _f(previous.get("filled_qty"))
            reported_qty = _f(order.get("filled_qty"))
            new_qty = max(reported_qty, old_qty)  # streams and REST may arrive out of order
            avg = order.get("filled_avg_price")
            if reported_qty < old_qty or avg is None:
                avg = previous.get("filled_avg_price")
            old_status = str(previous.get("status") or "").lower()
            new_status = str(order.get("status") or "unknown").lower()
            if old_status in TERMINAL_STATUSES and new_status not in TERMINAL_STATUSES:
                new_status = old_status
            return self._append(
                "ORDER_STATE", order_id=oid,
                status=new_status,
                filled_qty=new_qty, filled_avg_price=_f(avg) if avg is not None else None,
                delta_filled_qty=max(new_qty - old_qty, 0.0),
                updated_at=str(order.get("updated_at") or order.get("filled_at") or
                               dt.datetime.now(dt.timezone.utc).isoformat()))

    def pending_order_ids(self) -> list[str]:
        return [oid for oid, state in self.states().items()
                if str(state.get("status", "")).lower() not in TERMINAL_STATUSES]

    def active_exit(self, structure_id: str) -> dict | None:
        states = self.states()
        for oid, desc in self.descriptors().items():
            if desc.get("purpose") == "exit" and desc.get("structure_id") == structure_id:
                if states.get(oid, {}).get("status", "new").lower() not in TERMINAL_STATUSES:
                    return desc
        return None

    @staticmethod
    def _signed_fill(desc: dict, state: dict) -> tuple[float, float]:
        qty = _f(state.get("filled_qty"))
        raw = state.get("filled_avg_price")
        if raw is None or qty <= 0:
            return 0.0, 0.0
        sign = 1.0 if _f(desc.get("signed_limit_price")) >= 0 else -1.0
        return qty, abs(_f(raw)) * sign

    def structure_summaries(self) -> dict[str, dict]:
        descs, states = self.descriptors(), self.states()
        out: dict[str, dict] = {}
        for oid, desc in descs.items():
            sid = desc["structure_id"]
            s = out.setdefault(sid, {
                "structure_id": sid, "thesis_id": desc.get("thesis_id", ""),
                "underlying": desc.get("underlying", ""),
                "family": desc.get("family", "custom"), "legs": desc.get("legs", []),
                "max_loss_per_unit": _f(desc.get("max_loss_per_unit")),
                "entry_qty": 0.0, "entry_notional": 0.0,
                "exit_qty": 0.0, "exit_notional": 0.0})
            qty, signed = self._signed_fill(desc, states.get(oid, {}))
            key = "entry" if desc.get("purpose") == "entry" else "exit"
            s[f"{key}_qty"] += qty
            s[f"{key}_notional"] += qty * signed
        for s in out.values():
            s["ledger_open_qty"] = max(s["entry_qty"] - s["exit_qty"], 0.0)
            matched = min(s["entry_qty"], s["exit_qty"])
            entry_avg = s["entry_notional"] / s["entry_qty"] if s["entry_qty"] else 0.0
            exit_avg = s["exit_notional"] / s["exit_qty"] if s["exit_qty"] else 0.0
            s["realised_pnl"] = round((-entry_avg - exit_avg) * matched * 100.0, 2)
        return out

    def risk_snapshot(self, positions: list[dict]) -> dict:
        """Normalize broker legs into structures and derive durable risk totals."""
        remaining: dict[str, dict] = {}
        for p in positions:
            if p.get("asset_class") not in (None, "us_option"):
                continue
            qty = abs(_f(p.get("qty")))
            if qty <= 0:
                continue
            remaining[str(p["symbol"])] = {**p, "_remaining": qty}

        structures: list[dict] = []
        for summary in self.structure_summaries().values():
            wanted = int(summary["ledger_open_qty"])
            if wanted <= 0 or not summary["legs"]:
                continue
            available = []
            for leg in summary["legs"]:
                p = remaining.get(leg["symbol"])
                expected_side = "long" if leg["side"] == "buy" else "short"
                if not p or p.get("side") != expected_side:
                    available.append(0)
                else:
                    available.append(int(p["_remaining"] // int(leg["ratio_qty"])))
            qty = min([wanted, *available]) if available else 0
            if qty <= 0:
                continue
            leg_positions = []
            for leg in summary["legs"]:
                p = remaining[leg["symbol"]]
                p["_remaining"] -= qty * int(leg["ratio_qty"])
                leg_positions.append(p)
            structures.append({
                **summary, "qty": qty, "source": "ledger",
                "premium_at_risk": summary["max_loss_per_unit"] * qty,
                "unrealized_pl": sum(_f(p.get("unrealized_pl")) for p in leg_positions),
                "cost_basis": sum(_f(p.get("cost_basis")) for p in leg_positions)})

        for symbol, p in remaining.items():
            qty = int(p["_remaining"])
            if qty <= 0:
                continue
            meta = parse_occ_symbol(symbol)
            side = "buy" if p.get("side") == "long" else "sell"
            intent = "buy_to_open" if side == "buy" else "sell_to_open"
            cost = abs(_f(p.get("cost_basis"))) * qty / max(abs(_f(p.get("qty"))), 1.0)
            risk = cost if side == "buy" else float("inf")
            structures.append({
                "structure_id": f"orphan:{symbol}", "thesis_id": "", "source": "broker",
                "underlying": meta.underlying, "family": "orphan", "qty": qty,
                "legs": [{"symbol": symbol, "ratio_qty": 1, "side": side,
                          "position_intent": intent, "strike": meta.strike,
                          "option_type": meta.option_type, "expiry": meta.expiry.isoformat()}],
                "premium_at_risk": risk, "max_loss_per_unit": risk / qty,
                "unrealized_pl": _f(p.get("unrealized_pl")),
                "cost_basis": _f(p.get("cost_basis"))})

        realised_losses = sum(max(-_f(s.get("realised_pnl")), 0.0)
                              for s in self.structure_summaries().values())
        premium = sum(_f(s.get("premium_at_risk")) for s in structures)
        if any(s.get("premium_at_risk") == float("inf") for s in structures):
            premium = float("inf")
        return {"structures": structures, "premium_at_risk": premium,
                "realised_loss": round(realised_losses, 2)}
