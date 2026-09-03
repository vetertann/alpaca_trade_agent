#!/usr/bin/env python
"""Read-only panel for the agent.

A separate process on purpose: the agent itself listens on nothing, and this only
ever reads the JSONL trace it writes. It cannot place, cancel, or influence a trade.

    PYTHONPATH=src .venv/bin/python scripts/panel.py --run-dir .run/live --port 7001
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.brain.loop import session_state
from agent.config import ET, MEASUREMENT_END, WINDOW_OPEN

PANEL = Path(__file__).resolve().parents[1] / "src" / "agent" / "panel" / "index.html"
MAX_LOG = 60
MAX_CODE = 20_000
MAX_RECENT_EQUITY_POINTS = 400
MAX_FULL_EQUITY_POINTS = 1_600

# $ per million tokens. Only Anthropic reports the cache split, so only Anthropic is
# priced; everything else contributes tokens without a dollar figure.
PRICES = {"claude-opus-5": (5.00, 25.00), "claude-opus-4-8": (5.00, 25.00),
          "claude-sonnet-5": (2.00, 10.00), "claude-haiku-4-5": (1.00, 5.00)}
CACHE_WRITE, CACHE_READ = 1.25, 0.10


def _iter_records(path: Path):
    """Yield valid JSONL records without retaining or bulk-reading the trace.

    PREFLIGHT and PORTFOLIO records are deliberately rich and can be hundreds of
    kilobytes each.  Expanding the complete trace into a Python list made panel
    memory grow with runtime and eventually hit the systemd limit.  The panel only
    needs running aggregates and the latest snapshot, so streaming is sufficient.
    """
    if not path.exists():
        return
    with path.open(errors="replace") as source:
        for line in source:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def _records(path: Path) -> list[dict]:
    """Compatibility helper for small ledgers and tests."""
    return list(_iter_records(path))


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
    epoch = last.get("epoch_started_at")
    rows = [{"policy": k, **v, "benchmark": k == "flat_cash",
             "epoch_started_at": epoch}
            for k, v in (last.get("books") or {}).items()]
    return sorted(rows, key=lambda r: -r.get("return_pct", 0))


def _number(value) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _in_scored_window(point: dict) -> bool:
    """Whether an equity mark belongs to the official measurement window."""
    try:
        stamp = dt.datetime.fromisoformat(str(point["t"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ET)
        return WINDOW_OPEN <= stamp.astimezone(ET) <= MEASUREMENT_END
    except (KeyError, TypeError, ValueError):
        return False


def _downsample_equity(points: list[dict], max_points: int) -> list[dict]:
    """Bound a chart series while retaining its first, last, high, and low marks.

    Simple stride sampling can erase the short-lived spikes that matter most in an
    options equity chart.  Min/max buckets preserve both sides of each interval and
    retain chronological order while keeping the panel response small.
    """
    if max_points < 2:
        raise ValueError("max_points must be at least 2")
    if len(points) <= max_points:
        return points

    interior = points[1:-1]
    bucket_count = max(1, (max_points - 2) // 2)
    bucket_size = max(1, (len(interior) + bucket_count - 1) // bucket_count)
    sampled = [points[0]]
    for start in range(0, len(interior), bucket_size):
        bucket = interior[start:start + bucket_size]
        low = min(range(len(bucket)), key=lambda i: float(bucket[i]["v"]))
        high = max(range(len(bucket)), key=lambda i: float(bucket[i]["v"]))
        for index in sorted({low, high}):
            sampled.append(bucket[index])
    sampled.append(points[-1])
    return sampled


def _structure_label(position: dict) -> str:
    """Human label for a normalized ledger structure, never a fake OCC symbol."""
    if position.get("symbol"):
        return str(position["symbol"])
    legs = position.get("legs") or []
    underlying = str(position.get("underlying") or "structure")
    expiry = str(legs[0].get("expiry") or "") if legs else ""
    puts = sorted(float(l["strike"]) for l in legs if l.get("option_type") == "put")
    calls = sorted(float(l["strike"]) for l in legs if l.get("option_type") == "call")
    pieces = []
    if puts:
        pieces.append("/".join(_number(s) for s in puts) + "P")
    if calls:
        pieces.append("/".join(_number(s) for s in calls) + "C")
    strikes = "–".join(pieces)
    suffix = f" · {expiry[5:]}" if len(expiry) >= 10 else ""
    family = str(position.get("family") or "structure").replace("_", " ")
    return f"{underlying} {strikes or family}{suffix}"


def _portfolio_row(position: dict) -> dict:
    row = dict(position)
    row["symbol"] = _structure_label(position)
    if row.get("market_value") is None:
        # Alpaca defines unrealized P&L as market value minus cost basis. Old
        # reconciliation records predate the aggregate market_value field, so the
        # panel can still render them accurately immediately after deployment.
        row["market_value"] = (float(row.get("cost_basis") or 0)
                               + float(row.get("unrealized_pl") or 0))
    return row


def build_state(run_dir: Path) -> dict:
    now = dt.datetime.now(ET)

    equity_series, positions, starting = [], [], 100_000.0
    has_portfolio_marks = False
    cycles, model, profile, mode = set(), None, "?", "?"
    robust_risk_pct = scenario_risk_pct = None
    execution_control = {"entries_frozen": False, "latched": False, "blockers": []}
    portfolio_scenario_risk = {}
    action_triggers = []
    latest_portfolio = {}
    log: list[dict] = []
    usage = {"input": 0, "output": 0, "cached": 0, "cache_write": 0,
             "reasoning": 0, "calls": 0, "cost_usd": 0.0, "priced_calls": 0,
             "by_model": {}}

    refusal_events: set[tuple[str, str]] = set()
    submitted_ids: set[str] = set()
    fill_ids: set[str] = set()
    deterministic_exit_ids: set[str] = set()
    no_trades = 0
    incomplete_cycles = 0
    reconciliations = 0

    for r in _iter_records(run_dir / "trace.jsonl"):
        kind = r.get("kind")
        if r.get("cycle"):
            cycles.add(r["cycle"])

        # Proof counters are accumulated in the same streaming pass as the UI
        # state.  Keeping a second in-memory copy of every record is unnecessary.
        if kind == "OUTCOME" and str(r.get("outcome") or "").upper() == "NO_TRADE":
            no_trades += 1
        elif kind == "OUTCOME" and str(r.get("outcome") or "").upper() == "INCOMPLETE":
            incomplete_cycles += 1
        elif kind == "RECONCILE":
            reconciliations += 1
        elif kind == "VERIFICATION" and not r.get("passed", True):
            names = re.findall(r"^FAIL\s+([^:]+):", str(r.get("checklist") or ""), re.M)
            for name in names or ["verification"]:
                refusal_events.add((str(r.get("cycle") or r.get("seq")),
                                    name.strip()))
        elif kind == "NOTE" and r.get("message") == "action_trigger_blocked":
            for name in r.get("failed_gates") or ["action_trigger"]:
                refusal_events.add((str(r.get("trigger_id") or r.get("seq")),
                                    str(name)))
        elif kind == "ORDER" and r.get("status") in (
                "submitted", "submitted_close"):
            oid = str(r.get("order_id") or r.get("client_order_id") or r.get("seq"))
            submitted_ids.add(oid)
            reason = str(r.get("reason") or "").lower()
            if (r.get("status") == "submitted_close"
                    and (str(r.get("execution_path") or "").startswith("host_")
                         or reason.startswith("action trigger ")
                         or "time stop" in reason
                         or "profit target" in reason
                         or reason.startswith("adaptive executable-profit"))):
                deterministic_exit_ids.add(oid)
        elif kind == "FILL" and float(r.get("delta_filled_qty") or 0) > 0:
            fill_ids.add(str(r.get("order_id") or r.get("client_order_id")
                             or r.get("seq")))

        if kind == "NOTE" and r.get("message") == "started":
            model = r.get("model", model)
            profile = r.get("profile", profile)
            mode = r.get("mode", mode)
            robust_risk_pct = r.get("robust_risk_pct", robust_risk_pct)
            scenario_risk_pct = r.get("scenario_risk_pct", scenario_risk_pct)

        elif kind == "NOTE" and r.get("message") == "execution_control":
            execution_control = {
                "entries_frozen": bool(r.get("entries_frozen")),
                "latched": bool(r.get("latched")),
                "blockers": r.get("blockers") or [],
            }

        elif kind == "PREFLIGHT":
            b = r.get("bundle", {})
            execution_control = b.get("execution_control") or execution_control
            eq = (b.get("account") or {}).get("equity")
            starting = float((b.get("account") or {}).get("starting_equity") or starting)
            if eq:
                equity_series.append({"t": r["ts"], "v": float(eq)})
            positions = b.get("book") or positions

        elif kind == "RECONCILE" and r.get("equity"):
            equity_series.append({"t": r["ts"], "v": float(r["equity"])})
            if not has_portfolio_marks:
                positions = r.get("positions") or positions

        elif kind == "PORTFOLIO":
            snapshot = r.get("snapshot") or {}
            latest_portfolio = snapshot
            action_triggers = snapshot.get("action_triggers") or []
            portfolio_scenario_risk = (
                snapshot.get("portfolio_scenario_risk") or portfolio_scenario_risk)
            has_portfolio_marks = True
            if snapshot.get("equity") is not None:
                equity_series.append({"t": r["ts"], "v": float(snapshot["equity"])})
            # Prefer normalized structures over raw broker legs. This also keeps
            # the UI's labels and P&L aligned with what the agent can close.
            positions = snapshot.get("structures") or positions

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

        elif kind == "NOTE" and str(r.get("message") or "").startswith(
                "action_trigger_"):
            message = str(r.get("message")).removeprefix("action_trigger_")
            failures = r.get("failed_gates") or []
            detail = str(r.get("reason") or "")
            if failures:
                detail += (" · " if detail else "") + "failed gates: " + ", ".join(failures)
            log.append({"cycle": r.get("cycle"), "ts": r["ts"],
                        "kind": "TRIGGER_STATE",
                        "text": f"{r.get('trigger_id')} {message}"
                                + (f": {detail}" if detail else "")})

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

    refusal_counts = collections.Counter(name for _, name in refusal_events)
    proof = {
        "scope": "current_trace_file",
        "cycles": len(cycles),
        "no_trades": no_trades,
        "incomplete_cycles": incomplete_cycles,
        "gate_refusals": len(refusal_events),
        "gate_refusals_by_reason": dict(refusal_counts.most_common()),
        # A positive fill is conclusive evidence that an order was submitted even
        # when the submission predates the current process build and has no ORDER
        # envelope in this trace.  Keep the proof monotonic across upgrades.
        "submitted_orders": len(submitted_ids | fill_ids),
        "submission_count_basis": "unique ORDER submissions or positive FILL evidence",
        "filled_orders": len(fill_ids),
        "reconciliations": reconciliations,
        "deterministic_exits": len(deterministic_exit_ids),
        "open_executable_pnl": latest_portfolio.get(
            "total_executable_unrealized_pl"),
    }

    equity = equity_series[-1]["v"] if equity_series else None
    scored_equity = [point for point in equity_series if _in_scored_window(point)]
    # Old/development traces can sit outside the fixed competition dates. Keep the
    # panel useful for those traces while making the live chart precisely scoped.
    full_equity = scored_equity or equity_series
    return {"now": now.isoformat(), "profile": profile, "mode": mode, "model": model,
            "robust_risk_pct": robust_risk_pct,
            "scenario_risk_pct": scenario_risk_pct,
            "session": session_state(now), "cycles": len(cycles),
            "equity": equity, "starting": starting,
            "equity_series": equity_series[-MAX_RECENT_EQUITY_POINTS:],
            "equity_series_full": _downsample_equity(
                full_equity, MAX_FULL_EQUITY_POINTS),
            "positions": [_portfolio_row(p) for p in positions],
            "execution_control": execution_control,
            "portfolio_scenario_risk": portfolio_scenario_risk,
            "action_triggers": action_triggers,
            "proof": proof,
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
    ap.add_argument("--port", type=int, default=7001)
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
