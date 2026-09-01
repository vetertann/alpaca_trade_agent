"""Durable, continuously revalidated expiry-settlement authorizations."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
from pathlib import Path


class SettlementAuthorizationStore:
    """Append-only operator/model intent; live safety is evaluated elsewhere."""

    def __init__(self, path: str | Path = ".run/settlement_authorizations.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _append(self, kind: str, **fields) -> None:
        row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
               "kind": kind, **fields}
        line = json.dumps(row, sort_keys=True) + "\n"
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

    def current(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        lines = self.path.read_text().splitlines()
        out: dict[str, dict] = {}
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    break
                raise
            sid = str(row.get("structure_id") or "")
            if not sid:
                continue
            if row.get("kind") == "SETTLEMENT_AUTHORIZATION":
                out[sid] = {**row, "status": "active"}
            elif row.get("kind") == "SETTLEMENT_AUTHORIZATION_STATE" and sid in out:
                out[sid].update(row)
        return out

    def authorize(self, structure_id: str, *, min_short_distance_points: float,
                  reason: str) -> dict:
        sid = str(structure_id or "").strip()
        distance = float(min_short_distance_points)
        reason = str(reason or "").strip()
        if not sid or not reason:
            raise ValueError("structure_id and settlement reason are required")
        if not math.isfinite(distance) or distance <= 0:
            raise ValueError("min_short_distance_points must be finite and positive")
        self._append("SETTLEMENT_AUTHORIZATION", structure_id=sid,
                     min_short_distance_points=distance, reason=reason)
        return dict(self.current()[sid])

    def remove(self, structure_id: str, *, reason: str) -> dict:
        sid = str(structure_id or "").strip()
        reason = str(reason or "").strip()
        current = self.current().get(sid)
        if current is None:
            raise ValueError(f"unknown settlement authorization {sid!r}")
        if not reason:
            raise ValueError("settlement removal reason is required")
        if current.get("status") == "active":
            self._append("SETTLEMENT_AUTHORIZATION_STATE", structure_id=sid,
                         status="removed", reason=reason)
        return dict(self.current()[sid])

    def active(self, structure_id: str) -> dict | None:
        row = self.current().get(str(structure_id))
        return dict(row) if row and row.get("status") == "active" else None

    def observable(self) -> list[dict]:
        return [{key: row.get(key) for key in (
                    "structure_id", "status", "min_short_distance_points",
                    "reason", "ts")}
                for row in self.current().values()]
