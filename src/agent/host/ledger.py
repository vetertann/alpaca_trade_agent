"""Append-only execution ledger and broker-position reconciliation.

The broker is authoritative for current positions and order state.  The ledger
preserves structure membership and entry/exit cash flows that the positions
endpoint does not expose, so the two are deliberately reconciled rather than one
being treated as a replacement for the other.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from agent.host.contracts import parse_occ_symbol


TERMINAL_STATUSES = {"filled", "canceled", "cancelled", "expired", "rejected"}
EXECUTION_TERMINAL_STATUSES = TERMINAL_STATUSES | {"not_found"}
UNRESOLVED_EXECUTION_STATUSES = {"pre_submit", "unknown"}
RECONCILE_BACKOFF_SECONDS = (0.0, 2.0, 5.0, 15.0, 30.0, 60.0)
MIN_NOT_FOUND_AGE_SECONDS = 15.0
NOT_FOUND_CONFIRMATIONS = 2


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
        existed = self.path.exists()
        with self._lock, self.path.open("a") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        if not existed:
            directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
                     filled_qty: float = 0.0, filled_avg_price: float | None = None,
                     must_fill: bool = False, exit_intent_id: str = "") -> dict:
        return self._append(
            "ORDER", order_id=order_id, client_order_id=client_order_id,
            structure_id=structure_id, purpose=purpose, thesis_id=thesis_id,
            underlying=underlying, family=family, legs=legs, qty=int(qty),
            signed_limit_price=float(signed_limit_price),
            max_loss_per_unit=float(max_loss_per_unit), cycle_id=cycle_id,
            reason=reason, status=status, filled_qty=float(filled_qty),
            filled_avg_price=filled_avg_price, must_fill=bool(must_fill),
            exit_intent_id=str(exit_intent_id or ""))

    # ---- durable submission intent ---------------------------------------
    @staticmethod
    def action_key(purpose: str, structure_id: str) -> str:
        """One active action per structure and purpose, independent of repricing."""
        return f"{str(purpose).lower()}:{structure_id}"

    @staticmethod
    def request_fingerprint(request: dict) -> str:
        """Hash a caller-normalized broker request, not its incidental JSON layout."""
        blob = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def prepare_submission(self, *, client_order_id: str, request: dict,
                           structure_id: str, purpose: str, thesis_id: str,
                           underlying: str, family: str, legs: list[dict], qty: int,
                           signed_limit_price: float, max_loss_per_unit: float,
                           cycle_id: str | None, reason: str = "",
                           must_fill: bool = False, exit_intent_id: str = "",
                           request_fingerprint: str | None = None) -> dict:
        """Durably record exactly what will be sent before the broker call.

        `_append` flushes and fsyncs.  Callers must not submit if this raises.
        """
        with self._lock:
            if self.execution(client_order_id) is not None:
                raise ValueError(f"execution {client_order_id!r} is already recorded")
            occupied = self.active_action(purpose, structure_id)
            if occupied:
                raise ValueError(
                    f"action {self.action_key(purpose, structure_id)!r} is already active")
            return self._append(
                "PRE_SUBMIT", execution_id=client_order_id,
                client_order_id=client_order_id,
                action_key=self.action_key(purpose, structure_id),
                request_fingerprint=(request_fingerprint or
                                     self.request_fingerprint(request)), request=request,
                structure_id=structure_id, purpose=purpose, thesis_id=thesis_id,
                underlying=underlying, family=family, legs=legs, qty=int(qty),
                signed_limit_price=float(signed_limit_price),
                max_loss_per_unit=float(max_loss_per_unit), cycle_id=cycle_id,
                reason=reason, status="pre_submit", lookup_attempts=0,
                consecutive_404=0, must_fill=bool(must_fill),
                exit_intent_id=str(exit_intent_id or ""))

    # ---- durable mandatory exits -----------------------------------------
    def exit_intents(self) -> dict[str, dict]:
        """Latest must-be-flat intent for each reconciled structure."""
        out: dict[str, dict] = {}
        for rec in self.records():
            if rec.get("kind") == "EXIT_INTENT":
                sid = str(rec["structure_id"])
                out[sid] = {**rec, "created_at": rec["ts"],
                            "updated_at": rec["ts"]}
            elif rec.get("kind") == "EXIT_INTENT_STATE":
                sid = str(rec["structure_id"])
                if sid in out:
                    created = out[sid].get("created_at")
                    out[sid].update(rec)
                    out[sid]["created_at"] = created
                    out[sid]["updated_at"] = rec["ts"]
        return out

    def arm_exit_intent(self, *, structure_id: str, thesis_id: str, reason: str,
                        source: str, legacy_order_id: str = "") -> dict:
        """Persist an unconditional instruction to keep closing until flat."""
        with self._lock:
            current = self.exit_intents().get(str(structure_id))
            if current and current.get("status") == "active":
                return current
            return self._append(
                "EXIT_INTENT", exit_intent_id=(
                    f"mx_{hashlib.sha256(str(structure_id).encode()).hexdigest()[:20]}"),
                structure_id=str(structure_id), thesis_id=str(thesis_id or ""),
                reason=str(reason), source=str(source), status="active",
                attempts=0, last_order_id=str(legacy_order_id or ""))

    def record_exit_intent_state(self, structure_id: str, status: str,
                                 **fields: Any) -> dict:
        if str(structure_id) not in self.exit_intents():
            raise KeyError(f"unknown exit intent for {structure_id!r}")
        return self._append("EXIT_INTENT_STATE", structure_id=str(structure_id),
                            status=str(status), **fields)

    def active_exit_intents(self) -> list[dict]:
        return [row for row in self.exit_intents().values()
                if row.get("status") == "active"]

    def record_execution_state(self, client_order_id: str, status: str,
                               **fields: Any) -> dict:
        if self.execution(client_order_id) is None:
            raise KeyError(f"unknown execution {client_order_id!r}")
        return self._append(
            "EXECUTION_STATE", execution_id=client_order_id,
            client_order_id=client_order_id, status=str(status).lower(), **fields)

    def executions(self) -> dict[str, dict]:
        """Latest lifecycle state keyed by the durable client order ID."""
        out: dict[str, dict] = {}
        for rec in self.records():
            kind = rec.get("kind")
            if kind == "PRE_SUBMIT":
                eid = str(rec["execution_id"])
                out[eid] = {**rec, "created_at": rec["ts"],
                            "updated_at": rec["ts"]}
            elif kind == "EXECUTION_STATE":
                eid = str(rec.get("execution_id") or rec.get("client_order_id") or "")
                if eid in out:
                    created = out[eid].get("created_at")
                    out[eid].update(rec)
                    out[eid]["created_at"] = created
                    out[eid]["updated_at"] = rec["ts"]
        return out

    def execution(self, client_order_id: str) -> dict | None:
        return self.executions().get(client_order_id)

    def descriptor_by_client_id(self, client_order_id: str) -> dict | None:
        for desc in self.descriptors().values():
            if desc.get("client_order_id") == client_order_id:
                return desc
        return None

    def mark_lookup_404(self, client_order_id: str, *, now: dt.datetime | None = None,
                        min_age_s: float = MIN_NOT_FOUND_AGE_SECONDS,
                        confirmations: int = NOT_FOUND_CONFIRMATIONS) -> dict:
        """A fresh 404 is ambiguous; only aged repeated misses become NOT_FOUND."""
        now = now or dt.datetime.now(dt.timezone.utc)
        current = self.execution(client_order_id)
        if current is None:
            raise KeyError(client_order_id)
        created = dt.datetime.fromisoformat(str(current["created_at"]))
        age = max((now - created).total_seconds(), 0.0)
        attempts = int(current.get("lookup_attempts") or 0) + 1
        misses = int(current.get("consecutive_404") or 0) + 1
        status = "not_found" if age >= min_age_s and misses >= confirmations else "unknown"
        return self.record_execution_state(
            client_order_id, status, lookup_attempts=attempts,
            consecutive_404=misses, last_checked_at=now.isoformat(), age_seconds=age)

    def mark_lookup_error(self, client_order_id: str, error: str, *,
                          now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(dt.timezone.utc)
        current = self.execution(client_order_id) or {}
        return self.record_execution_state(
            client_order_id, "unknown",
            lookup_attempts=int(current.get("lookup_attempts") or 0) + 1,
            consecutive_404=0, last_checked_at=now.isoformat(), error=error)

    def unresolved_executions(self, *, now: dt.datetime | None = None,
                              due_only: bool = True) -> list[dict]:
        now = now or dt.datetime.now(dt.timezone.utc)
        rows = []
        for execution in self.executions().values():
            if execution.get("status") not in UNRESOLVED_EXECUTION_STATUSES:
                continue
            if due_only and execution.get("last_checked_at"):
                checked = dt.datetime.fromisoformat(str(execution["last_checked_at"]))
                attempts = int(execution.get("lookup_attempts") or 0)
                wait = RECONCILE_BACKOFF_SECONDS[min(
                    attempts, len(RECONCILE_BACKOFF_SECONDS) - 1)]
                if (now - checked).total_seconds() < wait:
                    continue
            rows.append(execution)
        # Risk-reducing exits reconcile first, then oldest first.
        return sorted(rows, key=lambda r: (r.get("purpose") != "exit", r["created_at"]))

    def entry_blockers(self) -> list[dict]:
        blockers = [r for r in self.executions().values()
                    if r.get("status") in UNRESOLVED_EXECUTION_STATUSES | {"mismatch"}]
        blockers.extend(self.execution_alerts().values())
        # A risk-reducing exit submitted by Tier 0 may race with a long Tier-2
        # reasoning cycle. Do not let that stale cycle add exposure until the exit
        # reaches terminal broker state.
        seen = {str(row.get("client_order_id")) for row in blockers}
        for execution in self.executions().values():
            if (execution.get("purpose") == "exit"
                    and str(execution.get("client_order_id")) not in seen
                    and self.active_action("exit", str(execution.get("structure_id")))
                    is not None):
                blockers.append(execution)
                seen.add(str(execution.get("client_order_id")))
        # An unconditional exit decision survives individual order cancellation.
        # Keep entries frozen until broker state confirms that structure is flat.
        blockers.extend({
            "client_order_id": row.get("last_client_order_id") or
                               row.get("exit_intent_id"),
            "purpose": "exit", "structure_id": row.get("structure_id"),
            "status": "mandatory_exit_pending",
        } for row in self.active_exit_intents())
        return blockers

    def record_execution_alert(self, client_order_id: str, *, order_id: str,
                               reason: str) -> dict:
        if client_order_id in self.execution_alerts():
            return self.execution_alerts()[client_order_id]
        return self._append(
            "EXECUTION_ALERT", client_order_id=client_order_id, order_id=order_id,
            purpose="unknown", structure_id="unknown", status="mismatch", reason=reason)

    def execution_alerts(self) -> dict[str, dict]:
        return {str(r["client_order_id"]): r for r in self.records()
                if r.get("kind") == "EXECUTION_ALERT"}

    def active_action(self, purpose: str, structure_id: str) -> dict | None:
        wanted = self.action_key(purpose, structure_id)
        order_states = self.states()
        for execution in reversed(list(self.executions().values())):
            if execution.get("action_key") != wanted:
                continue
            status = str(execution.get("status") or "pre_submit").lower()
            if status in EXECUTION_TERMINAL_STATUSES:
                continue
            oid = execution.get("order_id")
            if oid and str(order_states.get(str(oid), {}).get("status", "new")).lower() \
                    in TERMINAL_STATUSES:
                continue
            return execution
        # Backward compatibility for orders written before PRE_SUBMIT existed.
        for oid, desc in reversed(list(self.descriptors().items())):
            if (desc.get("purpose") == purpose
                    and desc.get("structure_id") == structure_id
                    and str(order_states.get(oid, {}).get("status", "new")).lower()
                    not in TERMINAL_STATUSES):
                return desc
        return None

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
        prepared = self.active_action("exit", structure_id)
        if prepared:
            return prepared
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
                "market_value": sum(_f(p.get("market_value")) for p in leg_positions),
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
                "market_value": _f(p.get("market_value")),
                "unrealized_pl": _f(p.get("unrealized_pl")),
                "cost_basis": _f(p.get("cost_basis"))})

        realised_losses = sum(max(-_f(s.get("realised_pnl")), 0.0)
                              for s in self.structure_summaries().values())
        premium = sum(_f(s.get("premium_at_risk")) for s in structures)
        if any(s.get("premium_at_risk") == float("inf") for s in structures):
            premium = float("inf")
        return {"structures": structures, "premium_at_risk": premium,
                "realised_loss": round(realised_losses, 2)}
