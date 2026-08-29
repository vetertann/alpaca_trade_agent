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
         "unrealized_pl": "20"},
        {"asset_class": "us_option", "symbol": LEGS[1]["symbol"],
         "qty": str(qty), "side": "short", "cost_basis": str(-140 * qty),
         "unrealized_pl": "10"},
    ]


def test_normalizes_legs_to_one_structure_and_derives_risk(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    order(ledger, "entry", "entry", 2.70, status="filled", filled=2, avg=2.70)
    state = ledger.risk_snapshot(positions(2))
    assert len(state["structures"]) == 1
    assert state["structures"][0]["qty"] == 2
    assert state["premium_at_risk"] == 540.0
    assert state["realised_loss"] == 0.0


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
