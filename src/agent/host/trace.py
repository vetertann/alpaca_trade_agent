"""JSONL decision trace, written from the first line of code.

Every generated program is stored verbatim and hashed, linked to the trigger that
caused it and the orders it produced.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path
from typing import Any


class Trace:
    def __init__(self, path: str | Path = ".run/trace.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cycle_id: str | None = None
        self.seq = 0
        self._lock = threading.RLock()

    def _write(self, kind: str, **fields: Any) -> dict:
        with self._lock:
            self.seq += 1
            rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "seq": self.seq, "cycle": self.cycle_id, "kind": kind, **fields}
            with self.path.open("a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        return rec

    # ---- lifecycle ---------------------------------------------------------
    def start_cycle(self, trigger: dict, bundle_hash: str) -> str:
        self.cycle_id = f"cy_{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}"
        self._write("TRIGGER", trigger=trigger, bundle_hash=bundle_hash)
        return self.cycle_id

    def preflight(self, bundle: dict) -> None:
        self._write("PREFLIGHT", bundle=bundle)

    def program(self, round_no: int, thought: str, code: str, provider: str,
                model: str, prompt_version: str, usage: dict, latency_s: float) -> None:
        self._write("PROGRAM", round=round_no, thought=thought, code=code,
                    code_sha=hashlib.sha256(code.encode()).hexdigest()[:16],
                    provider=provider, model=model, prompt_version=prompt_version,
                    usage=usage, latency_s=latency_s)

    def evidence(self, stdout: str, calls: list[dict], ok: bool, duration_s: float,
                 stderr: str = "", state_manifest: dict | None = None,
                 timed_out: bool = False) -> None:
        self._write("EVIDENCE", stdout=stdout[-16000:], calls=calls, ok=ok,
                    duration_s=duration_s, stderr=stderr[-4000:] if stderr else "",
                    timed_out=bool(timed_out), state_manifest=state_manifest or {})

    def verification(self, checklist: str, passed: bool) -> None:
        self._write("VERIFICATION", checklist=checklist, passed=passed)

    def order(self, result: dict) -> None:
        self._write("ORDER", **result)

    def fill(self, result: dict) -> None:
        # Ledger state records carry their own JSONL discriminator. It is evidence,
        # not the trace envelope's discriminator, and must not collide with
        # `_write(kind=...)` during asynchronous fill reconciliation.
        payload = {k: v for k, v in result.items()
                   if k not in ("ts", "seq", "cycle", "kind")}
        self._write("FILL", **payload)

    def reconcile(self, equity: float, positions: list, realised: float) -> None:
        self._write("RECONCILE", equity=equity, positions=positions, realised=realised)

    def portfolio(self, snapshot: dict) -> None:
        """A continuous mark, independent of the slower model decision cadence."""
        self._write("PORTFOLIO", snapshot=snapshot)

    def outcome(self, outcome: str, reason: str = "") -> None:
        self._write("OUTCOME", outcome=outcome, reason=reason)

    def note(self, message: str, **fields: Any) -> None:
        self._write("NOTE", message=message, **fields)

    def error(self, where: str, exc: BaseException) -> None:
        self._write("ERROR", where=where, error=type(exc).__name__, message=str(exc)[:4000])

    # ---- reading -----------------------------------------------------------
    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def cycles(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in self.records():
            out.setdefault(r.get("cycle") or "none", []).append(r)
        return out
