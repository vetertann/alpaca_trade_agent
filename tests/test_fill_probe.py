import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fill_probe.py"
_SPEC = importlib.util.spec_from_file_location("fill_probe", _PATH)
assert _SPEC and _SPEC.loader
fill_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fill_probe)


def test_verdict_uses_total_ratio_and_per_leg_structure_units():
    order = {
        "qty": "2", "filled_qty": "6", "filled_avg_price": "1.20",
        "legs": [
            {"symbol": "A", "ratio_qty": "1", "qty": "2", "filled_qty": "2"},
            {"symbol": "B", "ratio_qty": "2", "qty": "4", "filled_qty": "4"},
        ],
    }
    result = fill_probe.verdict(order, submitted_qty=2)
    assert result["denomination"] == "leg-contracts"
    assert result["total_ratio_qty"] == 3
    assert result["completed_structure_qty_from_legs"] == 2
    assert "leg count" not in result["impact"]


def test_marketable_credit_limit_moves_toward_an_easier_fill():
    legs = [
        {"symbol": "SHORT", "ratio_qty": "1", "side": "buy"},
        {"symbol": "LONG", "ratio_qty": "1", "side": "sell"},
    ]
    quotes = {"SHORT": {"ap": 1.10}, "LONG": {"bp": 3.00}}
    assert fill_probe.marketable_mleg_limit(legs, quotes) == -1.85


def test_closing_order_is_built_from_positions_and_reduces_ratios():
    positions = [
        {"symbol": "LONG", "qty": "2", "side": "long"},
        {"symbol": "SHORT", "qty": "4", "side": "short"},
        {"symbol": "UNRELATED", "qty": "9", "side": "long"},
    ]
    legs, qty = fill_probe.closing_order_from_positions(
        positions, {"LONG", "SHORT"})
    assert qty == 2
    assert [(leg["symbol"], leg["ratio_qty"], leg["position_intent"])
            for leg in legs] == [
                ("SHORT", "2", "buy_to_close"),
                ("LONG", "1", "sell_to_close"),
            ]


def test_flatten_cancels_a_nonterminal_close(monkeypatch):
    class Broker:
        def __init__(self):
            self.cancelled = []
            self.submitted = None
            self.status = "new"

        def positions(self):
            return [{"symbol": "LONG", "qty": "1", "side": "long"},
                    {"symbol": "SHORT", "qty": "1", "side": "short"}]

        def option_quotes(self, symbols):
            return {"LONG": {"bp": 3.00, "ap": 3.10},
                    "SHORT": {"bp": 1.00, "ap": 1.10}}

        def submit_mleg(self, legs, qty, limit, coid):
            self.submitted = (legs, qty, limit, coid)
            return {"id": "close-1", "status": "new"}

        def order(self, oid):
            return {"id": oid, "status": self.status, "filled_qty": "0"}

        def cancel(self, oid):
            self.cancelled.append(oid)
            self.status = "canceled"

    broker = Broker()
    monkeypatch.setattr(fill_probe, "wait_filled",
                        lambda rest, oid, seconds=60: rest.order(oid))
    final = fill_probe.flatten_probe_positions(broker, {"LONG", "SHORT"})
    assert broker.submitted[2] == -1.85
    assert broker.cancelled == ["close-1"]
    assert final["status"] == "canceled"
