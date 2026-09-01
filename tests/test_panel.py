import json
import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "agent_panel", Path(__file__).parents[1] / "scripts" / "panel.py")
_PANEL = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_PANEL)
build_state = _PANEL.build_state
read_shadow = _PANEL._shadow


def test_panel_exposes_the_instance_risk_variant(tmp_path):
    (tmp_path / "trace.jsonl").write_text(json.dumps({
        "ts": "2026-08-31T15:00:00+00:00", "kind": "NOTE",
        "message": "started", "profile": "dev", "mode": "execute",
        "model": "anthropic/claude-opus-5", "robust_risk_pct": 0.10,
        "scenario_risk_pct": 0.10,
    }) + "\n")

    state = build_state(tmp_path)

    assert state["profile"] == "dev"
    assert state["robust_risk_pct"] == 0.10
    assert state["scenario_risk_pct"] == 0.10


def test_normalized_structures_render_as_strategy_rows(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "ts": "2026-08-31T14:44:25+00:00",
        "kind": "RECONCILE",
        "cycle": "cycle-1",
        "equity": 99_700,
        "positions": [{
            "underlying": "SPY",
            "family": "iron_condor",
            "qty": 2,
            "cost_basis": -222,
            "unrealized_pl": -6,
            "legs": [
                {"strike": 759, "option_type": "put", "expiry": "2026-09-01"},
                {"strike": 762, "option_type": "put", "expiry": "2026-09-01"},
                {"strike": 768, "option_type": "call", "expiry": "2026-09-01"},
                {"strike": 771, "option_type": "call", "expiry": "2026-09-01"},
            ],
        }],
        "realised": 0,
    }) + "\n")

    row = build_state(tmp_path)["positions"][0]

    assert row["symbol"] == "SPY 759/762P–768/771C · 09-01"
    assert row["market_value"] == -228
    assert row["unrealized_pl"] == -6


def test_broker_leg_symbol_and_value_remain_unchanged(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "ts": "2026-08-31T14:40:39+00:00",
        "kind": "PREFLIGHT",
        "cycle": "cycle-1",
        "bundle": {
            "account": {"equity": 100_000, "starting_equity": 100_000},
            "book": [{"symbol": "QQQ260901C00717000", "qty": "4",
                      "market_value": "552", "unrealized_pl": "0"}],
        },
    }) + "\n")

    row = build_state(tmp_path)["positions"][0]

    assert row["symbol"] == "QQQ260901C00717000"
    assert row["market_value"] == "552"


def test_continuous_portfolio_marks_update_equity_and_strategy_rows(tmp_path):
    (tmp_path / "trace.jsonl").write_text(json.dumps({
        "ts": "2026-08-31T18:15:00+00:00", "kind": "PORTFOLIO", "snapshot": {
            "equity": 99_838.09,
            "structures": [{"structure_id": "sid-1", "underlying": "QQQ",
                            "family": "vertical_call", "qty": 1,
                            "market_value": -700, "unrealized_pl": -67,
                            "legs": [{"strike": 707, "option_type": "call",
                                      "expiry": "2026-09-01"},
                                     {"strike": 717, "option_type": "call",
                                      "expiry": "2026-09-01"}]}]}
    }) + "\n")

    state = build_state(tmp_path)

    assert state["equity"] == 99_838.09
    assert state["positions"][0]["symbol"] == "QQQ 707/717C · 09-01"


def test_panel_exposes_a_live_portfolio_scenario_breach(tmp_path):
    risk = {"status": "ok", "breached": True, "loss_dollars": 1700,
            "limit_dollars": 1500, "limit_pct_of_equity": 1.5}
    (tmp_path / "trace.jsonl").write_text(json.dumps({
        "ts": "2026-09-01T15:00:00+00:00", "kind": "PORTFOLIO",
        "snapshot": {"equity": 100_000, "structures": [],
                     "portfolio_scenario_risk": risk},
    }) + "\n")

    assert build_state(tmp_path)["portfolio_scenario_risk"] == risk


def test_panel_exposes_trigger_state_and_failed_host_gates(tmp_path):
    trigger = {
        "trigger_id": "t123", "purpose": "entry", "status": "blocked_risk",
        "condition": {"kind": "max_entry_debit", "value": 2.5},
        "last_evaluation_status": "blocked_risk",
        "last_gate_failures": ["portfolio_scenario"],
        "intent_summary": {"underlying": "SPY", "family": "vertical_call"},
        "seconds_remaining": 0,
    }
    rows = [
        {"ts": "2026-09-01T15:00:00+00:00", "kind": "PORTFOLIO",
         "snapshot": {"equity": 100_000, "structures": [],
                      "action_triggers": [trigger]}},
        {"ts": "2026-09-01T15:00:01+00:00", "kind": "NOTE",
         "message": "action_trigger_blocked", "trigger_id": "t123",
         "reason": "scenario cap", "failed_gates": ["portfolio_scenario"]},
    ]
    (tmp_path / "trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))

    state = build_state(tmp_path)

    assert state["action_triggers"][0]["status"] == "blocked_risk"
    assert state["action_triggers"][0]["last_gate_failures"] == [
        "portfolio_scenario"]
    events = [event for cycle in state["cycle_log"] for event in cycle["events"]]
    assert any(event["kind"] == "TRIGGER_STATE" and
               "portfolio_scenario" in event["text"] for event in events)


def test_panel_builds_compact_trace_proof_counters(tmp_path):
    rows = [
        {"ts": "2026-09-01T14:00:00+00:00", "kind": "TRIGGER",
         "cycle": "cycle-1", "trigger": {"name": "anchor"}},
        {"ts": "2026-09-01T14:00:01+00:00", "kind": "VERIFICATION",
         "cycle": "cycle-1", "passed": False,
         "checklist": "FAIL  portfolio_scenario: over cap\nPASS  spread: ok"},
        {"ts": "2026-09-01T14:00:02+00:00", "kind": "OUTCOME",
         "cycle": "cycle-1", "outcome": "NO_TRADE", "reason": "blocked"},
        {"ts": "2026-09-01T14:01:00+00:00", "kind": "ORDER",
         "status": "submitted_close", "order_id": "o1",
         "execution_path": "host_exit_sweep", "reason": "profit target"},
        {"ts": "2026-09-01T14:01:01+00:00", "kind": "FILL",
         "order_id": "o1", "delta_filled_qty": 1},
        {"ts": "2026-09-01T14:01:02+00:00", "kind": "RECONCILE",
         "equity": 100_010, "positions": [], "realised": 0},
        {"ts": "2026-09-01T14:01:03+00:00", "kind": "PORTFOLIO",
         "snapshot": {"equity": 100_010, "structures": [],
                      "total_executable_unrealized_pl": 12.5}},
    ]
    (tmp_path / "trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))

    proof = build_state(tmp_path)["proof"]

    assert proof == {
        "scope": "current_trace_file", "cycles": 1, "no_trades": 1,
        "incomplete_cycles": 0,
        "gate_refusals": 1,
        "gate_refusals_by_reason": {"portfolio_scenario": 1},
        "submitted_orders": 1,
        "submission_count_basis": "unique ORDER submissions or positive FILL evidence",
        "filled_orders": 1, "reconciliations": 1,
        "deterministic_exits": 1, "open_executable_pnl": 12.5,
    }


def test_panel_counts_fill_as_durable_submission_evidence(tmp_path):
    (tmp_path / "trace.jsonl").write_text(json.dumps({
        "ts": "2026-09-01T14:01:01+00:00", "kind": "FILL",
        "order_id": "older-order", "delta_filled_qty": 1,
    }) + "\n")

    proof = build_state(tmp_path)["proof"]

    assert proof["submitted_orders"] == 1
    assert proof["filled_orders"] == 1


def test_panel_counts_incomplete_protocol_separately_from_errors(tmp_path):
    rows = [
        {"ts": "2026-09-01T15:00:00+00:00", "kind": "TRIGGER",
         "cycle": "cycle-1", "trigger": {"name": "session_anchor"}},
        {"ts": "2026-09-01T15:00:01+00:00", "kind": "OUTCOME",
         "cycle": "cycle-1", "outcome": "INCOMPLETE",
         "reason": "safe pre-submit protocol exhausted"},
    ]
    (tmp_path / "trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))

    state = build_state(tmp_path)

    assert state["proof"]["incomplete_cycles"] == 1
    assert state["proof"]["no_trades"] == 0
    assert state["cycle_log"][0]["outcome"] == "INCOMPLETE"


def test_shadow_rows_identify_cash_benchmark_and_epoch(tmp_path):
    epoch = "2026-08-31T15:00:00+00:00"
    (tmp_path / "shadow.jsonl").write_text(json.dumps({
        "schema_version": 1, "ts": epoch, "epoch_started_at": epoch,
        "books": {
            "bull_call": {"return_pct": 0.2, "realised": 0, "open": 1, "total": 1},
            "flat_cash": {"return_pct": 0, "realised": 0, "open": 0, "total": 0},
        },
    }) + "\n")

    rows = read_shadow(tmp_path)

    cash = next(row for row in rows if row["policy"] == "flat_cash")
    assert cash["benchmark"] is True
    assert cash["epoch_started_at"] == epoch
