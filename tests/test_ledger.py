import datetime as dt

import pytest

from agent.host.ledger import ExecutionLedger


LEGS = [
    {"symbol": "SPY260903C00770000", "ratio_qty": 1, "side": "buy",
     "position_intent": "buy_to_open", "strike": 770.0,
     "option_type": "call", "expiry": "2026-09-03"},
    {"symbol": "SPY260903C00775000", "ratio_qty": 1, "side": "sell",
     "position_intent": "sell_to_open", "strike": 775.0,
     "option_type": "call", "expiry": "2026-09-03"},
]


def order(ledger, oid, purpose, limit_price, *, status="new", filled=0, avg=None):
    ledger.record_order(
        order_id=oid, client_order_id=f"co-{oid}", structure_id="spread-1",
        purpose=purpose, thesis_id="th_spy", underlying="SPY",
        family="vertical_call", legs=LEGS, qty=2,
        signed_limit_price=limit_price, max_loss_per_unit=270.0,
        cycle_id="cycle-1", status=status, filled_qty=filled,
        filled_avg_price=avg)


def positions(qty):
    return [
        {"asset_class": "us_option", "symbol": LEGS[0]["symbol"],
         "qty": str(qty), "side": "long", "cost_basis": str(410 * qty),
         "market_value": str(410 * qty + 20), "unrealized_pl": "20"},
        {"asset_class": "us_option", "symbol": LEGS[1]["symbol"],
         "qty": str(qty), "side": "short", "cost_basis": str(-140 * qty),
         "market_value": str(-140 * qty + 10), "unrealized_pl": "10"},
    ]


def prepare(ledger, coid="x-prepared", *, purpose="exit", limit=-2.0):
    return ledger.prepare_submission(
        client_order_id=coid,
        request={"order_class": "mleg", "client_order_id": coid,
                 "qty": "1", "type": "limit", "limit_price": str(limit),
                 "time_in_force": "day", "legs": LEGS},
        structure_id="spread-1", purpose=purpose, thesis_id="th_spy",
        underlying="SPY", family="vertical_call", legs=LEGS, qty=1,
        signed_limit_price=limit, max_loss_per_unit=270, cycle_id="cycle-1")


def test_normalizes_legs_to_one_structure_and_derives_risk(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    order(ledger, "entry", "entry", 2.70, status="filled", filled=2, avg=2.70)
    state = ledger.risk_snapshot(positions(2))
    assert len(state["structures"]) == 1
    assert state["structures"][0]["qty"] == 2
    assert state["structures"][0]["market_value"] == 570.0
    assert state["premium_at_risk"] == 540.0
    assert state["realised_loss"] == 0.0


def test_pre_submit_preserves_sizing_posture_for_restart_safe_one_shot(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    prepared = ledger.prepare_submission(
        client_order_id="x-terminal", request={}, structure_id="spread-1",
        purpose="entry", thesis_id="th_spy", underlying="SPY",
        family="vertical_call", legs=LEGS, qty=2,
        signed_limit_price=2.70, max_loss_per_unit=270.0,
        cycle_id="cycle-1", sizing_posture="terminal_push")

    assert prepared["sizing_posture"] == "terminal_push"
    assert ledger.executions()["x-terminal"]["sizing_posture"] == "terminal_push"


def test_partial_exit_realised_loss_and_restart_recovery(tmp_path):
    path = tmp_path / "execution.jsonl"
    ledger = ExecutionLedger(path)
    order(ledger, "entry", "entry", 2.70, status="filled", filled=2, avg=2.70)
    order(ledger, "exit", "exit", -2.00, status="new")
    first = ledger.record_state({"id": "exit", "status": "partially_filled",
                                 "filled_qty": "1", "filled_avg_price": "2.00"})
    duplicate = ledger.record_state({"id": "exit", "status": "partially_filled",
                                     "filled_qty": "1", "filled_avg_price": "2.00"})
    assert first["delta_filled_qty"] == 1
    assert duplicate["delta_filled_qty"] == 0

    restarted = ExecutionLedger(path)
    state = restarted.risk_snapshot(positions(1))
    assert state["structures"][0]["qty"] == 1
    assert state["premium_at_risk"] == 270.0
    assert state["realised_loss"] == 70.0
    assert restarted.pending_order_ids() == ["exit"]


def test_untracked_short_position_blocks_aggregate_risk(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    orphan = [{"asset_class": "us_option", "symbol": LEGS[1]["symbol"],
               "qty": "1", "side": "short", "cost_basis": "-140",
               "unrealized_pl": "0"}]
    state = ledger.risk_snapshot(orphan)
    assert state["structures"][0]["source"] == "broker"
    assert state["premium_at_risk"] == float("inf")


def test_restart_ignores_one_torn_trailing_append(tmp_path):
    path = tmp_path / "execution.jsonl"
    ledger = ExecutionLedger(path)
    order(ledger, "entry", "entry", 2.70, status="filled", filled=1, avg=2.70)
    with path.open("a") as fh:
        fh.write('{"kind":"ORDER_STATE"')
    restarted = ExecutionLedger(path)
    assert restarted.states()["entry"]["status"] == "filled"


def test_fill_state_never_regresses_on_out_of_order_update(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    order(ledger, "entry", "entry", 2.70)
    ledger.record_state({"id": "entry", "status": "partially_filled",
                         "filled_qty": "1", "filled_avg_price": "2.70"})
    stale = ledger.record_state({"id": "entry", "status": "new", "filled_qty": "0"})
    assert stale["filled_qty"] == 1
    assert stale["filled_avg_price"] == 2.70


def test_pre_submit_occupies_exit_action_before_broker_id_exists(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    prepared = prepare(ledger)
    assert prepared["status"] == "pre_submit"
    assert ledger.active_exit("spread-1")["client_order_id"] == "x-prepared"
    assert ledger.active_exit("spread-1").get("order_id") is None

    with pytest.raises(ValueError, match="already active"):
        prepare(ledger, "x-repriced", limit=-1.95)


def test_submitted_exit_freezes_new_entries_until_terminal(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    prepare(ledger)
    ledger.record_execution_state("x-prepared", "submitted", order_id="exit-1")

    assert ledger.entry_blockers()[0]["purpose"] == "exit"

    ledger.record_execution_state("x-prepared", "rejected", order_id="exit-1")
    assert ledger.entry_blockers() == []


def test_mandatory_exit_intent_survives_cancel_and_restart(tmp_path):
    path = tmp_path / "execution.jsonl"
    ledger = ExecutionLedger(path)
    intent = ledger.arm_exit_intent(
        structure_id="spread-1", thesis_id="th_spy",
        reason="scenario repair", source="portfolio_scenario_breach")
    ledger.record_exit_intent_state(
        "spread-1", "active", attempts=1, last_order_id="exit-1")

    restarted = ExecutionLedger(path)
    restored = restarted.active_exit_intents()[0]

    assert restored["exit_intent_id"] == intent["exit_intent_id"]
    assert restored["attempts"] == 1
    assert restarted.entry_blockers()[0]["status"] == "mandatory_exit_pending"

    restarted.record_exit_intent_state("spread-1", "filled_flat")
    assert restarted.active_exit_intents() == []
    assert restarted.entry_blockers() == []


def test_fresh_404_stays_unknown_until_aged_requery(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    prepared = prepare(ledger)
    created = dt.datetime.fromisoformat(prepared["ts"])

    first = ledger.mark_lookup_404("x-prepared", now=created + dt.timedelta(seconds=1))
    assert first["status"] == "unknown"
    second = ledger.mark_lookup_404("x-prepared", now=created + dt.timedelta(seconds=16))
    assert second["status"] == "not_found"
    assert ledger.active_exit("spread-1") is None


def test_lookup_transport_error_resets_consecutive_404_count(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    prepared = prepare(ledger)
    created = dt.datetime.fromisoformat(prepared["ts"])
    ledger.mark_lookup_404("x-prepared", now=created + dt.timedelta(seconds=16))
    ledger.mark_lookup_error("x-prepared", "503", now=created + dt.timedelta(seconds=20))
    state = ledger.mark_lookup_404("x-prepared", now=created + dt.timedelta(seconds=30))
    assert state["status"] == "unknown"
    assert state["consecutive_404"] == 1
