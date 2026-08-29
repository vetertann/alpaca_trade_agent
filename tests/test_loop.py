import datetime as dt
import pytest
from agent.config import ET
from agent.brain import loop
from agent.host.risk_params import DEFAULT as RP


def et(d, h, m):
    return dt.datetime(2026, 9, d, h, m, tzinfo=ET)


# --- session state -----------------------------------------------------------

def test_session_states():
    assert loop.session_state(et(1, 9, 0)) == "CLOSED"
    assert loop.session_state(et(1, 9, 35)) == "WARM_UP"
    assert loop.session_state(et(1, 12, 0)) == "ACTIVE"
    assert loop.session_state(et(1, 15, 50)) == "WINDING_DOWN"
    assert loop.session_state(et(1, 16, 30)) == "CLOSED"


def test_final_session_winds_down_earlier():
    """Thursday is the last scored session; its objective is final posture."""
    assert loop.session_state(et(3, 15, 10)) == "WINDING_DOWN"
    assert loop.session_state(et(1, 15, 10)) == "ACTIVE"


def test_entries_blocked_outside_window_and_states():
    assert not loop.entries_allowed(et(4, 10, 0))[0]      # Friday, outside
    assert not loop.entries_allowed(et(1, 9, 35))[0]      # warm-up
    assert loop.entries_allowed(et(1, 12, 0))[0]


# --- triggers ----------------------------------------------------------------

def test_anchor_fires_once_per_anchor():
    ts = loop.TriggerState()
    t = ts.evaluate(et(1, 9, 46), {}, [])
    assert t and t.name == "session_anchor"
    ts.record_cycle(et(1, 9, 46), {}, t)
    assert ts.evaluate(et(1, 9, 50), {}, []) is None
    t2 = ts.evaluate(et(1, 11, 1), {}, [])
    assert t2 and t2.name == "session_anchor"


def test_debounce_blocks_rapid_cycles():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0, "iv_rv_ratio": 1.0}})
    uni = {"SPY": {"spot": 103.0, "iv_rv_ratio": 1.0}}
    assert ts.evaluate(et(1, 12, 5), uni, [], {"SPY": 0.01}) is None    # within 10 min
    t = ts.evaluate(et(1, 12, 11), uni, [], {"SPY": 0.01})
    assert t and t.name == "underlying_move"


def test_move_trigger_needs_half_the_expected_daily_move():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    small = {"SPY": {"spot": 100.2}}
    assert ts.evaluate(et(1, 12, 30), small, [], {"SPY": 0.01}) is None
    big = {"SPY": {"spot": 100.8}}
    assert ts.evaluate(et(1, 12, 30), big, [], {"SPY": 0.01}).name == "underlying_move"


def test_volatility_shift_trigger():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0, "iv_rv_ratio": 1.00}})
    uni = {"SPY": {"spot": 100.0, "iv_rv_ratio": 1.15}}
    t = ts.evaluate(et(1, 12, 30), uni, [], {})
    assert t and t.name == "volatility_shift"


def test_cycle_cap_stops_escalation():
    ts = loop.TriggerState()
    ts.cycles_this_session = loop.MAX_CYCLES_PER_SESSION
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    ts.last_anchor_fired = loop.ANCHORS[-1]
    assert ts.evaluate(et(1, 12, 30), {"SPY": {"spot": 110.0}}, [], {"SPY": 0.01}) is None


def test_closed_session_never_triggers():
    ts = loop.TriggerState()
    assert ts.evaluate(et(1, 6, 0), {"SPY": {"spot": 100.0}}, []) is None


# --- exits -------------------------------------------------------------------

def test_profit_target_exit():
    pos = {"cost_basis": "1000", "unrealized_pl": "600"}
    due, why = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert due and "profit target" in why


def test_long_premium_has_no_drawdown_stop():
    """A stop here would sell the convexity the premium was bought to own."""
    pos = {"cost_basis": "1000", "unrealized_pl": "-900"}
    due, _ = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert not due


def test_short_premium_has_a_credit_multiple_stop():
    pos = {"cost_basis": "-500", "unrealized_pl": "-1100"}
    due, why = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert due and "short-premium stop" in why


def test_final_session_time_stop():
    pos = {"cost_basis": "1000", "unrealized_pl": "0"}
    due, why = loop.position_exit_due(pos, {}, et(3, 15, 30), RP)
    assert due and "time stop" in why


# --- trading-day awareness ---------------------------------------------------

def test_weekend_morning_is_closed_not_warm_up():
    """2026-08-29 is a Saturday. Time of day alone would call 09:34 WARM_UP."""
    saturday = dt.datetime(2026, 8, 29, 9, 34, tzinfo=ET)
    assert loop.session_state(saturday) == "CLOSED"


def test_holiday_closes_the_session():
    weekday = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    assert loop.session_state(weekday, trading_day=True) == "ACTIVE"
    assert loop.session_state(weekday, trading_day=False) == "CLOSED"


def test_entries_blocked_on_a_non_trading_day():
    weekday = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    assert loop.entries_allowed(weekday, trading_day=True)[0]
    assert not loop.entries_allowed(weekday, trading_day=False)[0]


def test_triggers_silent_on_a_non_trading_day():
    ts = loop.TriggerState()
    weekday = dt.datetime(2026, 9, 1, 11, 30, tzinfo=ET)
    assert ts.evaluate(weekday, {}, [], trading_day=False) is None
    assert ts.evaluate(weekday, {}, [], trading_day=True) is not None
