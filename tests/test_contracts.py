import datetime as dt

import pytest

from agent.host import gates
from agent.host.contracts import parse_occ_symbol, resolve_intent
from agent.host.risk_params import DEFAULT as RP
from agent.quant import structures as st
from agent.types import Leg, TradeIntent


SYMBOL = "SPY260903C00770000"
EXPIRY = dt.date(2026, 9, 3)


class Rest:
    def __init__(self, **changes):
        self.contract = {"symbol": SYMBOL, "underlying_symbol": "SPY",
                         "strike_price": "770", "type": "call",
                         "expiration_date": "2026-09-03", "tradable": True,
                         "status": "active", **changes}

    def option_contract(self, symbol):
        return self.contract


def intent(*, strike=770, underlying="SPY", side="buy"):
    position_intent = "buy_to_open" if side == "buy" else "sell_to_open"
    leg = Leg(SYMBOL, 1, side, position_intent, strike, "call", EXPIRY)
    return TradeIntent(underlying, "custom", (leg,), "th_contract", 1000)


def test_occ_symbol_is_parsed_host_side():
    meta = parse_occ_symbol(SYMBOL)
    assert (meta.underlying, meta.strike, meta.option_type, meta.expiry) == (
        "SPY", 770.0, "call", EXPIRY)


def test_resolve_intent_returns_broker_metadata():
    got = resolve_intent(Rest(), intent())
    assert got.legs[0].strike == 770.0 and got.underlying == "SPY"


def test_model_metadata_mismatch_is_rejected():
    with pytest.raises(ValueError, match="model metadata mismatch"):
        resolve_intent(Rest(), intent(strike=771))
    with pytest.raises(ValueError, match="underlying"):
        resolve_intent(Rest(), intent(underlying="QQQ"))


def test_inactive_contract_is_rejected():
    with pytest.raises(ValueError, match="not an active tradable"):
        resolve_intent(Rest(status="inactive", tradable=False), intent())


def test_net_short_call_is_unbounded_and_blocked():
    leg = intent(side="sell").legs[0]
    assert st.max_loss([leg], -1.0, 1) == st.UNBOUNDED
    result = gates.g_structure([leg], "SPY")
    assert not result.passed and "unbounded" in result.reason


def test_structure_rejects_nonpositive_ratio_and_mixed_expiry():
    first = intent().legs[0]
    zero = Leg(first.symbol, 0, first.side, first.position_intent,
               first.strike, first.option_type, first.expiry)
    assert not gates.g_structure([zero], "SPY").passed
    other = Leg("SPY260904C00775000", 1, "sell", "sell_to_open", 775,
                "call", dt.date(2026, 9, 4))
    assert not gates.g_structure([first, other], "SPY").passed


def test_structure_rejects_unreduced_ratios_and_generated_closes():
    first = intent().legs[0]
    second = Leg("SPY260903C00775000", 2, "sell", "sell_to_open", 775,
                 "call", EXPIRY)
    doubled = Leg(first.symbol, 2, first.side, first.position_intent,
                  first.strike, first.option_type, first.expiry)
    assert "lowest terms" in gates.g_structure([doubled, second], "SPY").reason
    closing = Leg(first.symbol, 1, "sell", "sell_to_close", first.strike,
                  first.option_type, first.expiry)
    assert "only open" in gates.g_structure([closing], "SPY").reason
