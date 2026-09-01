"""Durable, monotonic adaptive exit policies.

The model may arm or tighten a profit-protection rule.  Tier 0 owns observation,
high-water tracking and execution, so firing never waits for another model turn.
Hard loss and time exits live elsewhere and cannot be weakened through this store.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
from pathlib import Path


class ExitPolicyStore:
    def __init__(self, path: str | Path = ".run/exit_policies.jsonl"):
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

    def get(self, structure_id: str) -> dict | None:
        current = None
        for row in self.records():
            if str(row.get("structure_id")) != str(structure_id):
                continue
            if row.get("kind") == "EXIT_POLICY":
                current = dict(row)
                current.update({"high_water_profit": 0.0, "breach_count": 0})
            elif row.get("kind") == "EXIT_POLICY_STATE" and current is not None:
                current["high_water_profit"] = float(
                    row.get("high_water_profit") or 0)
                current["breach_count"] = int(row.get("breach_count") or 0)
                current["state_updated_at"] = row.get("ts")
        return current

    def set(self, structure_id: str, *, activation_profit: float,
            max_profit_giveback: float, minimum_locked_profit: float,
            confirmation_samples: int, hard_profit_target: float,
            reason: str) -> dict:
        # A policy write is two durable records (definition then state). Keep the
        # whole read/validate/write sequence atomic with respect to the Tier-0
        # observer, which runs in a different thread during model execution.
        with self._lock:
            return self._set_locked(
                structure_id, activation_profit=activation_profit,
                max_profit_giveback=max_profit_giveback,
                minimum_locked_profit=minimum_locked_profit,
                confirmation_samples=confirmation_samples,
                hard_profit_target=hard_profit_target, reason=reason)

    def _set_locked(self, structure_id: str, *, activation_profit: float,
                    max_profit_giveback: float, minimum_locked_profit: float,
                    confirmation_samples: int, hard_profit_target: float,
                    reason: str) -> dict:
        sid = str(structure_id or "").strip()
        reason = str(reason or "").strip()
        values = [activation_profit, max_profit_giveback,
                  minimum_locked_profit, hard_profit_target]
        if not sid:
            raise ValueError("structure_id is required")
        if not reason:
            raise ValueError("adaptive exit reason is required")
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("adaptive exit values must be finite")
        activation = round(float(activation_profit), 2)
        giveback = round(float(max_profit_giveback), 2)
        locked = round(float(minimum_locked_profit), 2)
        hard = round(float(hard_profit_target), 2)
        samples = int(confirmation_samples)
        if activation <= 0 or giveback <= 0 or locked < 0:
            raise ValueError("activation/giveback must be positive and locked profit non-negative")
        if locked >= activation:
            raise ValueError("minimum_locked_profit must be below activation_profit")
        if giveback > activation - locked:
            raise ValueError(
                "max_profit_giveback cannot exceed activation minus locked profit")
        if hard > 0 and activation >= hard:
            raise ValueError("adaptive activation must be below the hard profit target")
        if not 1 <= samples <= 6:
            raise ValueError("confirmation_samples must be between 1 and 6")

        old = self.get(sid)
        if old:
            # Once risk is delegated to Tier 0, a later model may only make the
            # rule more protective. It cannot move the goalposts after a drawdown.
            if activation > float(old["activation_profit"]):
                raise ValueError("activation_profit may only be lowered")
            if giveback > float(old["max_profit_giveback"]):
                raise ValueError("max_profit_giveback may only be lowered")
            if locked < float(old["minimum_locked_profit"]):
                raise ValueError("minimum_locked_profit may only be raised")
            if samples > int(old["confirmation_samples"]):
                raise ValueError("confirmation_samples may only be lowered")

        self._append(
            "EXIT_POLICY", structure_id=sid, activation_profit=activation,
            max_profit_giveback=giveback, minimum_locked_profit=locked,
            confirmation_samples=samples, hard_profit_target=hard, reason=reason)
        # Preserve a previously observed high-water across policy tightening.
        high = float((old or {}).get("high_water_profit") or 0)
        self._append("EXIT_POLICY_STATE", structure_id=sid,
                     high_water_profit=high, breach_count=0)
        return self.view(sid) or {}

    def observe(self, structure_id: str, executable_profit: float | None,
                *, quotes_valid: bool) -> dict | None:
        # Reading the policy, advancing its high-water and persisting the new
        # breach count are one transition. Otherwise a simultaneous tightening
        # could be followed by a stale state append from the observer.
        with self._lock:
            return self._observe_locked(
                structure_id, executable_profit, quotes_valid=quotes_valid)

    def _observe_locked(self, structure_id: str,
                        executable_profit: float | None, *,
                        quotes_valid: bool) -> dict | None:
        policy = self.get(structure_id)
        if policy is None:
            return None
        if not quotes_valid or executable_profit is None:
            return self._view(policy, triggered=False, observation_valid=False)
        pnl = round(float(executable_profit), 2)
        high = max(float(policy.get("high_water_profit") or 0), pnl)
        armed = high >= float(policy["activation_profit"])
        threshold = max(float(policy["minimum_locked_profit"]),
                        high - float(policy["max_profit_giveback"]))
        breached = armed and pnl <= threshold
        count = int(policy.get("breach_count") or 0) + 1 if breached else 0
        if (high != float(policy.get("high_water_profit") or 0)
                or count != int(policy.get("breach_count") or 0)):
            self._append("EXIT_POLICY_STATE", structure_id=str(structure_id),
                         high_water_profit=high, breach_count=count)
            policy["high_water_profit"], policy["breach_count"] = high, count
        return self._view(
            policy, triggered=count >= int(policy["confirmation_samples"]),
            observation_valid=True, executable_profit=pnl)

    def view(self, structure_id: str) -> dict | None:
        policy = self.get(structure_id)
        return self._view(policy, triggered=False, observation_valid=None) \
            if policy else None

    @staticmethod
    def _view(policy: dict, *, triggered: bool,
              observation_valid: bool | None, executable_profit: float | None = None) -> dict:
        high = float(policy.get("high_water_profit") or 0)
        threshold = max(float(policy["minimum_locked_profit"]),
                        high - float(policy["max_profit_giveback"]))
        return {
            "structure_id": policy["structure_id"],
            "activation_profit": float(policy["activation_profit"]),
            "max_profit_giveback": float(policy["max_profit_giveback"]),
            "minimum_locked_profit": float(policy["minimum_locked_profit"]),
            "confirmation_samples": int(policy["confirmation_samples"]),
            "hard_profit_target": float(policy["hard_profit_target"]),
            "reason": policy["reason"],
            "high_water_profit": high,
            "armed": high >= float(policy["activation_profit"]),
            "current_trigger_profit": round(threshold, 2),
            "breach_count": int(policy.get("breach_count") or 0),
            "observation_valid": observation_valid,
            "executable_profit": executable_profit,
            "triggered": bool(triggered),
        }
