import datetime as dt

import pytest

from agent.host.action_triggers import (
    ActionTriggerStore, entry_condition, intent_from_dict, intent_to_dict)
from agent.host.execution import Executor
from agent.host.ledger import ExecutionLedger
from agent.host.risk_params import DEFAULT as RP
from agent.types import Leg, TradeIntent


NOW = dt.datetime(2026, 9, 1, 15, 0, tzinfo=dt.timezone.utc)
EXPIRY = dt.date(2026, 9, 3)
ACCOUNT = {"id": "expected", "equity": "100000", "options_trading_level": 3,
           "options_buying_power": "100000", "trading_blocked": False,
           "account_blocked": False}


def intent():
    return TradeIntent(
        "SPY", "vertical_call", (
            Leg("SPY260903C00770000", 1, "buy", "buy_to_open",
                770, "call", EXPIRY),
            Leg("SPY260903C00775000", 1, "sell", "sell_to_open",
                775, "call", EXPIRY)),
        "thesis-1", 270)


class Rest:
    profile = "dev"

    def __init__(self):
        self.quotes = {
            "SPY260903C00770000": {"bp": 4.0, "ap": 4.1},
            "SPY260903C00775000": {"bp": 1.4, "ap": 1.5},
        }
        self.submitted = []

    def option_quotes(self, symbols):
        return {symbol: {**self.quotes[symbol], "t": NOW.isoformat()}
                for symbol in symbols}

    def account(self):
        return dict(ACCOUNT)

    def option_contract(self, symbol):
        leg = next(row for row in intent().legs if row.symbol == symbol)
        return {"symbol": symbol, "underlying_symbol": "SPY",
                "strike_price": str(leg.strike), "type": leg.option_type,
                "expiration_date": leg.expiry.isoformat(), "tradable": True,
                "status": "active"}

    def submit_mleg(self, legs, qty, limit_price, coid, tif="day"):
        self.submitted.append((legs, qty, limit_price, coid))
        return {"id": "order-1", "status": "new"}


def executor(tmp_path=None):
    rest = Rest()
    ledger = ExecutionLedger(tmp_path / "execution.jsonl") if tmp_path else None
    return Executor(rest, RP, "competition", mode="execute", ledger=ledger,
                    expected_account_id="expected"), rest


def test_intent_round_trip_preserves_exact_leg_metadata():
    assert intent_from_dict(intent_to_dict(intent())) == intent()


def test_store_is_durable_removable_and_expires(tmp_path):
    path = tmp_path / "action_triggers.jsonl"
    store = ActionTriggerStore(path)
    row = store.set_entry(
        intent(), condition=entry_condition(max_entry_debit=2.6),
        valid_for_seconds=60, reference_spot=772,
        max_spot_drift_pct=0.3, evidence={"candidate": "exact"},
        reason="buy only at reviewed economics", now=NOW)

    assert row["purpose"] == "entry" and row["seconds_remaining"] == 60
    assert ActionTriggerStore(path).active(NOW)[0]["trigger_id"] == row["trigger_id"]
    removed = ActionTriggerStore(path).remove(row["trigger_id"], "premise changed")
    assert removed["status"] == "cancelled"
    assert ActionTriggerStore(path).active(NOW) == []

    exit_row = store.set_exit(
        "sid-1", min_executable_profit=40, valid_for_seconds=5,
        reason="take the quoted gain", now=NOW)
    assert store.active(NOW)[0]["trigger_id"] == exit_row["trigger_id"]
    assert store.expire_due(NOW + dt.timedelta(seconds=6)) == [exit_row["trigger_id"]]
    assert store.current()[exit_row["trigger_id"]]["status"] == "expired"


def test_entry_condition_requires_one_positive_boundary():
    with pytest.raises(ValueError, match="exactly one"):
        entry_condition()
    with pytest.raises(ValueError, match="exactly one"):
        entry_condition(max_entry_debit=2, min_entry_credit=1)
    with pytest.raises(ValueError, match="positive"):
        entry_condition(max_entry_debit=0)


def test_execute_if_refuses_stale_economics_before_submission(tmp_path):
    ex, rest = executor(tmp_path)
    ex.begin_cycle("cycle")
    ex.begin_program(1)
    condition = {"kind": "max_entry_debit", "value": 2.80}
    first = ex.execute(
        intent(), economic_condition=condition, authorization_seconds=30,
        equity=100_000, now=NOW)
    assert first["status"] == "staged"

    # The spread becomes more expensive while the model reviews the draft.
    rest.quotes["SPY260903C00770000"]["ap"] = 4.4
    ex.begin_program(2)
    second = ex.execute(
        intent(), economic_condition=condition, authorization_seconds=30,
        equity=100_000, now=NOW + dt.timedelta(seconds=5))

    assert second["status"] == "condition_not_met"
    assert second["limit_price"] == pytest.approx(3.0)
    assert rest.submitted == []
    assert ex.latest_staged is None


def test_execute_if_authorization_does_not_extend_across_review(tmp_path):
    ex, rest = executor(tmp_path)
    ex.begin_cycle("cycle")
    ex.begin_program(1)
    condition = {"kind": "max_entry_debit", "value": 2.80}
    assert ex.execute(
        intent(), economic_condition=condition, authorization_seconds=5,
        equity=100_000, now=NOW)["status"] == "staged"
    ex.begin_program(2)
    out = ex.execute(
        intent(), economic_condition=condition, authorization_seconds=5,
        equity=100_000, now=NOW + dt.timedelta(seconds=6))
    assert out["status"] == "condition_expired"
    assert rest.submitted == []


def test_conditional_close_uses_fresh_whole_structure_executable_profit(tmp_path):
    ex, rest = executor(tmp_path)
    structure = {
        "structure_id": "sid-1", "qty": 1, "cost_basis": 270,
        "underlying": "SPY", "family": "vertical_call", "thesis_id": "thesis-1",
        "legs": Executor._legs_json(intent()), "max_loss_per_unit": 270,
    }
    missed = ex.close_structure(
        structure, reason="take profit only", now=NOW,
        min_executable_profit=0, client_order_seed="trigger-1")
    assert missed["status"] == "condition_not_met"
    assert missed["executable_profit"] == pytest.approx(-20)
    assert rest.submitted == []

    accepted = ex.close_structure(
        structure, reason="bounded loss accepted", now=NOW,
        min_executable_profit=-25, client_order_seed="trigger-1")
    assert accepted["status"] == "submitted_close"
    assert len(rest.submitted) == 1
    duplicate = ex.close_structure(
        structure, reason="bounded loss accepted", now=NOW,
        min_executable_profit=-25, client_order_seed="trigger-1")
    assert duplicate["status"] == "already_pending"
    assert len(rest.submitted) == 1
