import datetime as dt
import pytest
from agent.types import Leg
from agent.quant import structures as st

EXP = dt.date(2026, 9, 3)


def leg(strike, kind, side, ratio=1, expiry=EXP):
    intent = "buy_to_open" if side == "buy" else "sell_to_open"
    return Leg(f"SPY{strike:.0f}{kind[0].upper()}", ratio, side, intent, strike, kind, expiry)


def test_long_call_max_loss_is_premium():
    legs = [leg(770, "call", "buy")]
    assert st.max_loss(legs, net_price=4.00) == pytest.approx(400.0)
    assert st.max_profit(legs, net_price=4.00) == st.UNBOUNDED


def test_bull_call_spread():
    """Buy 770 / sell 775 for 2.65 debit: risk 265, reward 235, width 500."""
    legs = [leg(770, "call", "buy"), leg(775, "call", "sell")]
    assert st.max_loss(legs, 2.65) == pytest.approx(265.0)
    assert st.max_profit(legs, 2.65) == pytest.approx(235.0)
    assert st.strike_width(legs) == pytest.approx(500.0)


def test_dominated_spread_has_no_profit():
    """Net debit at or above the width -- guaranteed loss at every outcome.

    A spread priced to negative maximum profit must be rejected.
    """
    legs = [leg(770, "call", "buy"), leg(775, "call", "sell")]
    assert st.max_profit(legs, net_price=5.00) == pytest.approx(0.0)
    assert st.max_profit(legs, net_price=6.00) < 0


def test_credit_spread_loss_exceeds_credit():
    """Sell 770 / buy 775 call for 1.50 credit: loss runs to width minus credit."""
    legs = [leg(770, "call", "sell"), leg(775, "call", "buy")]
    assert st.max_loss(legs, net_price=-1.50) == pytest.approx(350.0)
    assert st.max_profit(legs, net_price=-1.50) == pytest.approx(150.0)
    assert st.breakevens(legs, net_price=-1.50) == pytest.approx([771.5])


def test_iron_condor():
    """760/765 put spread + 775/780 call spread, 1.80 credit, 5-wide wings."""
    legs = [leg(760, "put", "buy"), leg(765, "put", "sell"),
            leg(775, "call", "sell"), leg(780, "call", "buy")]
    assert st.max_loss(legs, net_price=-1.80) == pytest.approx(320.0)
    assert st.max_profit(legs, net_price=-1.80) == pytest.approx(180.0)
    assert st.breakevens(legs, net_price=-1.80) == pytest.approx([763.2, 776.8])


def test_long_straddle():
    legs = [leg(770, "call", "buy"), leg(770, "put", "buy")]
    assert st.max_loss(legs, net_price=8.00) == pytest.approx(800.0)
    assert st.breakevens(legs, net_price=8.00) == pytest.approx([762.0, 778.0])


def test_large_long_call_premium_finds_breakeven_beyond_plotting_range():
    legs = [leg(100, "call", "buy")]
    assert st.breakevens(legs, net_price=150.0) == pytest.approx([250.0])


def test_quantity_scales_risk():
    legs = [leg(770, "call", "buy"), leg(775, "call", "sell")]
    assert st.max_loss(legs, 2.65, qty=3) == pytest.approx(795.0)


def test_universal_spread_rule_offsets_within_expiry():
    """Alpaca's example: two offsetting call spreads net to zero worst case."""
    legs = [leg(100, "call", "buy"), leg(110, "call", "sell"),
            leg(200, "call", "buy"), leg(190, "call", "sell")]
    # Traditional pairing would charge for the 190/200 credit spread; the
    # piecewise-payoff method sees the offset.
    assert st.max_loss(legs, net_price=0.0) == pytest.approx(0.0)


def test_payoff_curve_monotone_for_long_call():
    legs = [leg(770, "call", "buy")]
    curve = st.payoff_curve(legs, 4.00, points=20)
    assert curve[0][1] == pytest.approx(-400.0)
    assert curve[-1][1] > curve[0][1]
