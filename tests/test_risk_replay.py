import json

import pytest

from agent.host.risk_replay import load_events


def test_load_events_joins_and_validates_parent_child_fill(tmp_path):
    prepared = {
        "kind": "PRE_SUBMIT", "client_order_id": "a1", "purpose": "entry",
        "structure_id": "s1", "underlying": "SPY", "family": "vertical_call",
        "legs": [
            {"symbol": "C100", "side": "buy", "position_intent": "buy_to_open",
             "ratio_qty": 1, "strike": 100, "option_type": "call",
             "expiry": "2026-09-03"},
            {"symbol": "C105", "side": "sell", "position_intent": "sell_to_open",
             "ratio_qty": 1, "strike": 105, "option_type": "call",
             "expiry": "2026-09-03"},
        ],
    }
    (tmp_path / "execution.jsonl").write_text(json.dumps(prepared) + "\n")
    order = {
        "id": "o1", "client_order_id": "a1", "status": "filled",
        "filled_qty": "2", "filled_avg_price": "1.25",
        "submitted_at": "2026-08-31T14:00:00Z",
        "filled_at": "2026-08-31T14:00:01Z",
        "legs": [
            {"symbol": "C100", "filled_qty": "2", "filled_avg_price": "2.00"},
            {"symbol": "C105", "filled_qty": "2", "filled_avg_price": "0.75"},
        ],
    }
    (tmp_path / "nested_orders.json").write_text(json.dumps([order]))
    rows = load_events(tmp_path, "sample")
    assert len(rows) == 1
    assert rows[0]["parent_child_price_difference"] == 0
    assert rows[0]["qty"] == 2


def test_load_events_rejects_leg_contract_denominator_mismatch(tmp_path):
    prepared = {
        "kind": "PRE_SUBMIT", "client_order_id": "a1", "purpose": "entry",
        "structure_id": "s1", "underlying": "SPY", "family": "vertical_call",
        "legs": [{"symbol": "C100", "side": "buy", "position_intent": "buy_to_open",
                  "ratio_qty": 1, "strike": 100, "option_type": "call",
                  "expiry": "2026-09-03"}],
    }
    (tmp_path / "execution.jsonl").write_text(json.dumps(prepared) + "\n")
    order = {
        "id": "o1", "client_order_id": "a1", "status": "filled",
        "filled_qty": "2", "filled_avg_price": "1",
        "submitted_at": "2026-08-31T14:00:00Z",
        "legs": [{"symbol": "C100", "filled_qty": "4", "filled_avg_price": "1"}],
    }
    (tmp_path / "nested_orders.json").write_text(json.dumps([order]))
    with pytest.raises(ValueError, match="child qty"):
        load_events(tmp_path, "sample")
