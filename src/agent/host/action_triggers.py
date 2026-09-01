"""Durable one-shot price triggers owned by the host.

The model authorizes an exact economic condition; this store persists that
authorization while the host watches faster than another reasoning turn.  These
rules are deliberately separate from mandatory loss/deadline exits, which cannot
be cancelled through this API.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import threading
import uuid
from pathlib import Path

from agent.types import Leg, TradeIntent

ACTIVE = {"active", "firing"}
TERMINAL = {"fired", "cancelled", "expired", "failed", "blocked_risk"}
MAX_ACTIVE = 4
MIN_ENTRY_TTL_SECONDS = 5
MAX_ENTRY_TTL_SECONDS = 120


def intent_to_dict(intent: TradeIntent) -> dict:
    return {
        "underlying": intent.underlying, "family": intent.family,
        "thesis_id": intent.thesis_id, "risk_budget": intent.risk_budget,
        "note": intent.note,
        "legs": [{"symbol": leg.symbol, "ratio_qty": leg.ratio_qty,
                  "side": leg.side, "position_intent": leg.position_intent,
                  "strike": leg.strike, "option_type": leg.option_type,
                  "expiry": leg.expiry.isoformat()} for leg in intent.legs],
    }


def intent_from_dict(raw: dict) -> TradeIntent:
    return TradeIntent(
        underlying=str(raw["underlying"]), family=str(raw.get("family") or "custom"),
        thesis_id=str(raw["thesis_id"]), risk_budget=float(raw.get("risk_budget") or 0),
        note=str(raw.get("note") or ""),
        legs=tuple(Leg(
            symbol=str(row["symbol"]), ratio_qty=int(row.get("ratio_qty") or 1),
            side=str(row["side"]), position_intent=str(row["position_intent"]),
            strike=float(row["strike"]), option_type=str(row["option_type"]),
            expiry=dt.date.fromisoformat(str(row["expiry"])))
            for row in raw["legs"]),
    )


def entry_condition(*, max_entry_debit=None, min_entry_credit=None) -> dict:
    supplied = [max_entry_debit is not None, min_entry_credit is not None]
    if sum(supplied) != 1:
        raise ValueError("supply exactly one of max_entry_debit or min_entry_credit")
    name = "max_entry_debit" if max_entry_debit is not None else "min_entry_credit"
    value = float(max_entry_debit if max_entry_debit is not None else min_entry_credit)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return {"kind": name, "value": round(value, 2)}


class ActionTriggerStore:
    def __init__(self, path: str | Path = ".run/action_triggers.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _append(self, kind: str, **fields) -> dict:
        rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
               "kind": kind, **fields}
        line = json.dumps(rec, sort_keys=True, default=str) + "\n"
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
                    break
                raise
        return out

    def current(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in self.records():
            trigger_id = str(row.get("trigger_id") or "")
            if not trigger_id:
                continue
            if row.get("kind") == "ACTION_TRIGGER":
                out[trigger_id] = dict(row)
                out[trigger_id]["status"] = "active"
            elif row.get("kind") == "ACTION_TRIGGER_STATE" and trigger_id in out:
                out[trigger_id].update({k: v for k, v in row.items()
                                        if k not in ("kind", "trigger_id")})
                out[trigger_id]["status"] = str(row.get("status") or
                                                  out[trigger_id]["status"])
        return out

    def active(self, now: dt.datetime | None = None) -> list[dict]:
        now = now or dt.datetime.now(dt.timezone.utc)
        rows = []
        for row in self.current().values():
            if row.get("status") not in ACTIVE:
                continue
            expires = dt.datetime.fromisoformat(str(row["expires_at"]))
            if now >= expires:
                continue
            rows.append(self._view(row, now))
        return sorted(rows, key=lambda row: (row["expires_at"], row["trigger_id"]))

    def observable(self, now: dt.datetime | None = None,
                   recent_terminal_seconds: float = 600.0) -> list[dict]:
        """Active rules plus recent terminal outcomes for prompts and the panel."""
        now = now or dt.datetime.now(dt.timezone.utc)
        rows = []
        for row in self.current().values():
            status = str(row.get("status") or "")
            if status in ACTIVE:
                expires = dt.datetime.fromisoformat(str(row["expires_at"]))
                if now < expires:
                    rows.append(self._view(row, now))
                continue
            if status not in TERMINAL:
                continue
            try:
                updated = dt.datetime.fromisoformat(str(row.get("ts")))
                age = (now - updated).total_seconds()
            except (TypeError, ValueError):
                continue
            # The append timestamp is taken a few milliseconds after the caller's
            # immutable observation timestamp; tolerate that harmless ordering.
            if -5 <= age <= float(recent_terminal_seconds):
                rows.append(self._view(row, now))
        return sorted(rows, key=lambda row: (
            row.get("status") not in ACTIVE,
            row.get("expires_at") or "", row["trigger_id"]))

    def expire_due(self, now: dt.datetime | None = None) -> list[str]:
        now = now or dt.datetime.now(dt.timezone.utc)
        expired = []
        with self._lock:
            for trigger_id, row in self.current().items():
                if row.get("status") in ACTIVE and now >= dt.datetime.fromisoformat(
                        str(row["expires_at"])):
                    self._append("ACTION_TRIGGER_STATE", trigger_id=trigger_id,
                                 status="expired", reason="authorization expired")
                    expired.append(trigger_id)
        return expired

    def set_entry(self, intent: TradeIntent, *, condition: dict,
                  valid_for_seconds: float, reference_spot: float,
                  max_spot_drift_pct: float, evidence: dict, reason: str,
                  now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(dt.timezone.utc)
        ttl = float(valid_for_seconds)
        drift = float(max_spot_drift_pct)
        reason = str(reason or "").strip()
        if not MIN_ENTRY_TTL_SECONDS <= ttl <= MAX_ENTRY_TTL_SECONDS:
            raise ValueError("entry trigger valid_for_seconds must be between 5 and 120")
        if not reason:
            raise ValueError("entry trigger reason is required")
        if not math.isfinite(reference_spot) or reference_spot <= 0:
            raise ValueError("entry trigger reference spot must be positive")
        if not math.isfinite(drift) or not 0 < drift <= 1.0:
            raise ValueError("max_spot_drift_pct must be in (0, 1.0]")
        raw_intent = intent_to_dict(intent)
        action_hash = hashlib.sha256(json.dumps(
            {"intent": raw_intent, "condition": condition}, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()[:24]
        with self._lock:
            self.expire_due(now)
            for row in self.active(now):
                if row.get("purpose") == "entry" and row.get("action_hash") == action_hash:
                    return row
            if len(self.active(now)) >= MAX_ACTIVE:
                raise ValueError(f"at most {MAX_ACTIVE} action triggers may be active")
            trigger_id = "t" + uuid.uuid4().hex[:23]
            self._append(
                "ACTION_TRIGGER", trigger_id=trigger_id, purpose="entry",
                action_hash=action_hash, intent=raw_intent, condition=condition,
                evidence=evidence, reference_spot=round(float(reference_spot), 6),
                max_spot_drift_pct=drift, reason=reason,
                expires_at=(now + dt.timedelta(seconds=ttl)).isoformat())
            return self._view(self.current()[trigger_id], now)

    def set_exit(self, structure_id: str, *, min_executable_profit: float,
                 valid_for_seconds: float, reason: str,
                 now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(dt.timezone.utc)
        sid = str(structure_id or "").strip()
        threshold = float(min_executable_profit)
        ttl = float(valid_for_seconds)
        reason = str(reason or "").strip()
        if not sid or not reason:
            raise ValueError("structure_id and exit trigger reason are required")
        if not math.isfinite(threshold):
            raise ValueError("min_executable_profit must be finite")
        if not 5 <= ttl <= 6 * 60 * 60:
            raise ValueError("exit trigger valid_for_seconds must be between 5s and 6h")
        with self._lock:
            self.expire_due(now)
            for row in self.active(now):
                if row.get("purpose") == "exit" and row.get("structure_id") == sid:
                    self.remove(row["trigger_id"], "replaced by a newer exit trigger")
            if len(self.active(now)) >= MAX_ACTIVE:
                raise ValueError(f"at most {MAX_ACTIVE} action triggers may be active")
            trigger_id = "t" + uuid.uuid4().hex[:23]
            self._append(
                "ACTION_TRIGGER", trigger_id=trigger_id, purpose="exit",
                structure_id=sid, condition={"kind": "min_executable_profit",
                                             "value": round(threshold, 2)},
                reason=reason,
                expires_at=(now + dt.timedelta(seconds=ttl)).isoformat())
            return self._view(self.current()[trigger_id], now)

    def remove(self, trigger_id: str, reason: str) -> dict:
        reason = str(reason or "").strip()
        with self._lock:
            row = self.current().get(str(trigger_id))
            if row is None:
                raise ValueError(f"unknown action trigger {trigger_id!r}")
            if not reason:
                raise ValueError("trigger removal reason is required")
            if row.get("status") not in ACTIVE:
                return self._view(row, dt.datetime.now(dt.timezone.utc))
            self._append("ACTION_TRIGGER_STATE", trigger_id=str(trigger_id),
                         status="cancelled", reason=reason)
            return self._view(self.current()[str(trigger_id)],
                              dt.datetime.now(dt.timezone.utc))

    def state(self, trigger_id: str, status: str, **fields) -> dict:
        if status not in ACTIVE | TERMINAL:
            raise ValueError(f"invalid action trigger state {status!r}")
        with self._lock:
            if str(trigger_id) not in self.current():
                raise ValueError(f"unknown action trigger {trigger_id!r}")
            self._append("ACTION_TRIGGER_STATE", trigger_id=str(trigger_id),
                         status=status, **fields)
            return self._view(self.current()[str(trigger_id)],
                              dt.datetime.now(dt.timezone.utc))

    @staticmethod
    def _view(row: dict, now: dt.datetime) -> dict:
        expires = dt.datetime.fromisoformat(str(row["expires_at"]))
        keep = {key: row.get(key) for key in (
            "trigger_id", "purpose", "structure_id", "action_hash", "condition",
            "reason", "expires_at", "status", "reference_spot", "max_spot_drift_pct",
            "last_observed_value", "last_observed_at", "last_evaluation_status",
            "last_evaluation_reason", "last_gate_failures", "last_evaluated_at",
            "result", "escalation_queued", "escalation_suppressed_reason", "ts")
            if row.get(key) is not None}
        keep["seconds_remaining"] = round(max((expires - now).total_seconds(), 0), 1)
        if row.get("intent"):
            raw = row["intent"]
            keep["intent_summary"] = {
                "underlying": raw.get("underlying"), "family": raw.get("family"),
                "thesis_id": raw.get("thesis_id"), "risk_budget": raw.get("risk_budget"),
                "legs": [leg.get("symbol") for leg in raw.get("legs") or []],
            }
        return keep
