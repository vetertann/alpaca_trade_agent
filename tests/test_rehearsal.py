import datetime as dt

from agent.config import ET
from agent.host.execution import Executor
from agent.host.ledger import ExecutionLedger
from agent.host.risk_params import DEFAULT as RP
from agent.run import Agent


LEGS = [
    {"symbol": "SPY260903C00770000", "ratio_qty": 1, "side": "buy",
     "position_intent": "buy_to_open", "strike": 770.0,
     "option_type": "call", "expiry": "2026-09-03"},
    {"symbol": "SPY260903C00775000", "ratio_qty": 1, "side": "sell",
     "position_intent": "sell_to_open", "strike": 775.0,
     "option_type": "call", "expiry": "2026-09-03"},
]
DEV_ACCOUNT_ID = "test-development-account"


class RehearsalBroker:
    profile = "dev"

    def __init__(self):
        self._positions = [
            {"asset_class": "us_option", "symbol": LEGS[0]["symbol"], "qty": "1",
             "side": "long", "cost_basis": "270", "unrealized_pl": "0"},
            {"asset_class": "us_option", "symbol": LEGS[1]["symbol"], "qty": "1",
             "side": "short", "cost_basis": "0", "unrealized_pl": "0"},
        ]
        self._orders = {}
        self.submissions = []
        self.cancellations = []

    def account(self):
        return {"id": DEV_ACCOUNT_ID, "options_trading_level": 3,
                "options_buying_power": "100000", "trading_blocked": False,
                "account_blocked": False}

    def positions(self):
        return list(self._positions)

    def option_quotes(self, symbols):
        quotes = {LEGS[0]["symbol"]: {"bp": 3.00, "ap": 3.10},
                  LEGS[1]["symbol"]: {"bp": 1.00, "ap": 1.10}}
        return {s: quotes[s] for s in symbols}

    def submit_mleg(self, legs, qty, limit_price, coid, tif="day"):
        oid = f"exit-{len(self.submissions) + 1}"
        self.submissions.append({"id": oid, "legs": legs, "qty": qty,
                                 "limit_price": limit_price, "coid": coid})
        self._orders[oid] = {"id": oid, "status": "new", "filled_qty": "0",
                             "filled_avg_price": None}
        return dict(self._orders[oid])

    def submit_single(self, *args, **kwargs):
        raise AssertionError("the tracked spread must close atomically")

    def order(self, order_id):
        return dict(self._orders[order_id])

    def cancel(self, order_id):
        self.cancellations.append(order_id)
        self._orders[order_id]["status"] = "canceled"

    def fill(self, order_id, qty="1", price="2.00"):
        self._orders[order_id] |= {"status": "filled", "filled_qty": qty,
                                   "filled_avg_price": price}
        self._positions = []


class Trace:
    def __init__(self):
        self.orders = []
        self.errors = []

    def note(self, *args, **kwargs):
        pass

    def order(self, result):
        self.orders.append(result)

    def fill(self, result):
        pass

    def error(self, where, exc):
        self.errors.append((where, exc))


class Theses:
    def get(self, _):
        return None


def test_full_restart_and_forced_liquidation_rehearsal(tmp_path):
    """Submission -> restart-safe pending state -> fill -> flat final book."""
    path = tmp_path / "execution.jsonl"
    ledger = ExecutionLedger(path)
    ledger.record_order(
        order_id="entry-1", client_order_id="entry-co", structure_id="spread-1",
        purpose="entry", thesis_id="th_spy", underlying="SPY",
        family="vertical_call", legs=LEGS, qty=1, signed_limit_price=2.70,
        max_loss_per_unit=270, cycle_id="entry-cycle", status="filled",
        filled_qty=1, filled_avg_price=2.70)
    broker = RehearsalBroker()

    agent = Agent.__new__(Agent)
    agent.ledger = ledger
    agent.executor = Executor(broker, RP, "dev", mode="execute", ledger=ledger,
                              expected_account_id=DEV_ACCOUNT_ID)
    agent.rest = broker
    agent.params = RP
    agent.trace = Trace()
    agent.theses = Theses()

    final_wind_down = dt.datetime(2026, 9, 3, 15, 46, tzinfo=ET)
    acted = agent.sweep_exits(now=final_wind_down)
    assert acted and broker.submissions
    submission = broker.submissions[0]
    assert [leg["position_intent"] for leg in submission["legs"]] == [
        "buy_to_close", "sell_to_close"]

    # A restarted process sees the durable pending exit and does not duplicate it.
    restarted_ledger = ExecutionLedger(path)
    restarted = Executor(broker, RP, "dev", mode="execute", ledger=restarted_ledger,
                         expected_account_id=DEV_ACCOUNT_ID)
    structure = restarted_ledger.risk_snapshot(broker.positions())["structures"][0]
    assert restarted.close_structure(structure, reason="restart retry")["status"] == \
        "already_pending"

    broker.fill(submission["id"], price="2.00")
    updates = restarted.reconcile_orders()
    assert updates[0]["status"] == "filled" and updates[0]["delta_filled_qty"] == 1
    final = ExecutionLedger(path).risk_snapshot(broker.positions())
    assert final["structures"] == []
    assert final["premium_at_risk"] == 0
    assert final["realised_loss"] == 70
