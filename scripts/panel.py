#!/usr/bin/env python
"""Read-only panel for the agent.

A separate process on purpose: the agent itself listens on nothing, and this only
ever reads the JSONL trace it writes. It cannot place, cancel, or influence a trade.

    PYTHONPATH=src .venv/bin/python scripts/panel.py --run-dir .run/live --port 3001
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.brain.loop import session_state
from agent.config import ET

PANEL = Path(__file__).resolve().parents[1] / "src" / "agent" / "panel" / "index.html"
MAX_LOG = 60
MAX_CODE = 20_000

# $ per million tokens. Only Anthropic reports the cache split, so only Anthropic is
# priced; everything else contributes tokens without a dollar figure.
PRICES = {"claude-opus-5": (5.00, 25.00), "claude-opus-4-8": (5.00, 25.00),
          "claude-sonnet-5": (2.00, 10.00), "claude-haiku-4-5": (1.00, 5.00)}
CACHE_WRITE, CACHE_READ = 1.25, 0.10


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _shadow(run_dir: Path) -> list[dict]:
    """Latest line of the shadow ledger: fixed policies run against the same quotes."""
    path = run_dir / "shadow.jsonl"
    if not path.exists():
        return []
    last = None
    for line in path.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                pass
    if not last:
        return []
    rows = [{"policy": k, **v} for k, v in (last.get("books") or {}).items()]
    return sorted(rows, key=lambda r: -r.get("return_pct", 0))


def build_state(run_dir: Path) -> dict:
    recs = _records(run_dir / "trace.jsonl")
    now = dt.datetime.now(ET)

    equity_series, positions, starting = [], [], 100_000.0
    cycles, model, profile, mode = set(), None, "?", "?"
    log: list[dict] = []
    usage = {"input": 0, "output": 0, "cached": 0, "cache_write": 0,
             "reasoning": 0, "calls": 0, "cost_usd": 0.0, "priced_calls": 0,
             "by_model": {}}

    for r in recs:
        kind = r.get("kind")
        if r.get("cycle"):
            cycles.add(r["cycle"])

        if kind == "NOTE" and r.get("message") == "started":
            model = r.get("model", model)
            profile = r.get("profile", profile)
            mode = r.get("mode", mode)

        elif kind == "PREFLIGHT":
            b = r.get("bundle", {})
            eq = (b.get("account") or {}).get("equity")
            if eq:
                equity_series.append({"t": r["ts"], "v": float(eq)})
            positions = b.get("book") or positions

        elif kind == "RECONCILE" and r.get("equity"):
            equity_series.append({"t": r["ts"], "v": float(r["equity"])})
            positions = r.get("positions") or positions

        elif kind == "TRIGGER":
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "TRIGGER",
                        "trigger": (r.get("trigger") or {}).get("name"),
                        "text": (r.get("trigger") or {}).get("detail", "")})

        elif kind == "PROGRAM":
            u = r.get("usage") or {}
            m = r.get("model", "")
            usage["calls"] += 1
            for k in ("input", "output", "cached", "cache_write", "reasoning"):
                usage[k] += int(u.get(k) or 0)
            row = usage["by_model"].setdefault(
                m, {"calls": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0})
            row["calls"] += 1
            row["input"] += int(u.get("input") or 0)
            row["output"] += int(u.get("output") or 0)
            row["cached"] += int(u.get("cached") or 0)
            if m in PRICES:
                pin, pout = PRICES[m]
                c = (int(u.get("input") or 0) * pin
                     + int(u.get("cache_write") or 0) * pin * CACHE_WRITE
                     + int(u.get("cached") or 0) * pin * CACHE_READ
                     + int(u.get("output") or 0) * pout) / 1e6
                usage["cost_usd"] += c
                usage["priced_calls"] += 1
                row["cost_usd"] += c
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "THOUGHT",
                        "model": m, "text": r.get("thought", "")})
            # The program is the artifact. Recording it to disk and hiding it from
            # the operator defeats the point of a code agent.
            code = r.get("code") or ""
            if code:
                log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "PROGRAM",
                            "text": code[:MAX_CODE],
                            "meta": f"round {r.get('round')} · {m} · "
                                    f"{r.get('latency_s')}s · sha {r.get('code_sha','')}"})

        elif kind == "EVIDENCE":
            calls = r.get("calls") or []
            names: dict[str, int] = {}
            for c in calls:
                key = f"{c.get('ns')}.{c.get('fn')}"
                names[key] = names.get(key, 0) + 1
            summary = ", ".join(f"{k}×{v}" if v > 1 else k for k, v in names.items())
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "EVIDENCE",
                        "text": (r.get("stdout") or "").strip() or "(no output)",
                        "meta": f"{len(calls)} capability calls · {r.get('duration_s')}s"
                                + (f" · {summary}" if summary else "")})
            if not r.get("ok") and r.get("stderr"):
                log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "TRACEBACK",
                            "text": (r.get("stderr") or "").strip()})

        elif kind == "VERIFICATION":
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "VERIFICATION",
                        "text": r.get("checklist", "")})

        elif kind == "ORDER":
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "ORDER",
                        "text": json.dumps({k: v for k, v in r.items()
                                            if k in ("status", "order_id", "qty",
                                                     "limit_price", "max_loss")},
                                           indent=None)})

        elif kind == "FILL":
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "FILL",
                        "text": json.dumps({k: v for k, v in r.items()
                                            if k not in ("ts", "seq", "cycle", "kind")})})

        elif kind == "OUTCOME":
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "OUTCOME", "outcome": r.get("outcome"),
                        "text": r.get("reason", "")})

        elif kind == "NOTE" and r.get("message") == "provider_fallback":
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "FALLBACK",
                        "text": "answered by " + str(r.get("answered_by")) + "\n"
                                + "\n".join(r.get("skipped") or [])})

        elif kind == "ERROR":
            log.append({"cycle": r.get("cycle"), "ts": r["ts"], "kind": "ERROR",
                        "text": f"{r.get('where')}: {r.get('message','')}"})


    # Group by cycle: newest cycle first, but chronological inside it, so a thought
    # is never shown after the outcome it produced.
    order, grouped = [], {}
    for e in log:
        cid = e.get("cycle") or "_"
        if cid not in grouped:
            grouped[cid] = {"cycle": cid, "ts": e["ts"], "trigger": None,
                            "outcome": None, "events": []}
            order.append(cid)
        g = grouped[cid]
        if e["kind"] == "TRIGGER":
            g["trigger"] = e.get("trigger") or e.get("text")
        elif e["kind"] == "OUTCOME":
            g["outcome"] = e.get("outcome")
        g["events"].append(e)

    cycles_out = [grouped[c] for c in reversed(order)][:12]

    equity = equity_series[-1]["v"] if equity_series else None
    return {"now": now.isoformat(), "profile": profile, "mode": mode, "model": model,
            "session": session_state(now), "cycles": len(cycles),
            "equity": equity, "starting": starting,
            "equity_series": equity_series[-400:], "positions": positions,
            "usage": usage, "shadow": _shadow(run_dir), "cycle_log": cycles_out}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *a, run_dir: Path, **kw):
        self.run_dir = run_dir
        super().__init__(*a, **kw)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                       # noqa: N802
        if self.path.startswith("/api/state"):
            try:
                body = json.dumps(build_state(self.run_dir), default=str).encode()
            except Exception as exc:                # a broken trace must not 500 the panel
                body = json.dumps({"error": str(exc)[:200]}).encode()
            self._send(200, body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PANEL.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:                      # noqa: N802
        self._send(405, b"read only", "text/plain")

    def log_message(self, *a) -> None:              # quiet
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=".run/live")
    ap.add_argument("--port", type=int, default=3001)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 by default; use 0.0.0.0 only behind a firewall rule")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    srv = ThreadingHTTPServer((args.host, args.port),
                              partial(Handler, run_dir=run_dir))
    print(f"panel  http://{args.host}:{args.port}   reading {run_dir}/trace.jsonl")
    print("read-only: it cannot place, cancel, or influence a trade")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
