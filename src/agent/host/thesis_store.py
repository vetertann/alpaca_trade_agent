"""Append-only thesis store.

Every position traces to a thesis, and every thesis carries its exit conditions in
a form the deterministic watcher can evaluate without a model. Cycles read this
first, which stops the agent re-deriving what it already holds.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from agent.types import Thesis


class ThesisStore:
    def __init__(self, path: str | Path = ".run/theses.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._by_id: dict[str, Thesis] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            d["opened_at"] = dt.datetime.fromisoformat(d["opened_at"])
            self._by_id[d["thesis_id"]] = Thesis(**d)

    def _append(self, t: Thesis) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(t.to_json()) + "\n")

    def new_id(self, underlying: str, when: dt.datetime | None = None) -> str:
        when = when or dt.datetime.now(dt.timezone.utc)
        return f"th_{when:%Y%m%d_%H%M}_{underlying.lower()}_{uuid.uuid4().hex[:4]}"

    def open(self, hypothesis: str, underlying: str, *, exit_profit: str,
             exit_invalidation: str, exit_time: str, exit_news: str = "",
             evidence_refs: list[str] | None = None,
             gates: dict[str, str] | None = None) -> Thesis:
        t = Thesis(self.new_id(underlying), dt.datetime.now(dt.timezone.utc), hypothesis,
                   exit_profit, exit_invalidation, exit_time, exit_news,
                   evidence_refs or [], [], gates or {})
        self._by_id[t.thesis_id] = t
        self._append(t)
        return t

    def get(self, thesis_id: str) -> Thesis | None:
        return self._by_id.get(thesis_id)

    def list(self, status: str | None = "open") -> list[Thesis]:
        return [t for t in self._by_id.values() if status is None or t.status == status]

    def close(self, thesis_id: str, *, reason: str, realised: float | None = None) -> Thesis:
        """How a thesis ended is the only feedback the agent ever gets."""
        t = self._by_id[thesis_id]
        t.status = "closed"
        t.notes.append(f"closed: {reason}"
                       + (f" (realised ${realised:,.0f})" if realised is not None else ""))
        self._append(t)
        return t

    def outcomes(self, limit: int = 10) -> list[dict]:
        """Closed theses, most recent first, compact enough for the bundle."""
        closed = [t for t in self._by_id.values() if t.status != "open"]
        closed.sort(key=lambda t: t.opened_at, reverse=True)
        out = []
        for t in closed[:limit]:
            end = next((n for n in reversed(t.notes) if "closed:" in n), "")
            out.append({"thesis_id": t.thesis_id, "hypothesis": t.hypothesis[:180],
                        "opened_at": t.opened_at.isoformat(timespec="minutes"),
                        "ended": end.split("closed:", 1)[-1].strip()})
        return out

    def update(self, thesis_id: str, *, note: str = "", status: str | None = None,
               order_ids: list[str] | None = None,
               gates: dict[str, str] | None = None) -> Thesis:
        t = self._by_id[thesis_id]
        if note:
            t.notes.append(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {note}")
        if status:
            t.status = status
        if order_ids:
            t.order_ids += order_ids
        if gates:
            t.gates |= gates
        self._append(t)
        return t

    def has_open_exposure(self, underlying: str) -> bool:
        return any(t.status == "open" and underlying.lower() in t.thesis_id
                   for t in self._by_id.values())
