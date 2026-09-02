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
    assert ts.evaluate(et(1, 12, 4), uni, [], {"SPY": 0.01}) is None
    t = ts.evaluate(et(1, 12, 5), uni, [], {"SPY": 0.01})
    assert t and t.name == "underlying_move"


def test_move_trigger_needs_half_the_expected_daily_move():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    small = {"SPY": {"spot": 100.2}}
    assert ts.evaluate(
        et(1, 12, 19), small, [], {"SPY": 0.01}, portfolio_risk_pct=0.04
    ) is None
    big = {"SPY": {"spot": 100.8}}
    assert ts.evaluate(
        et(1, 12, 20), big, [], {"SPY": 0.01}, portfolio_risk_pct=0.04
    ).name == "underlying_move"


def test_volatility_shift_trigger():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0, "iv_rv_ratio": 1.00}})
    uni = {"SPY": {"spot": 100.0, "iv_rv_ratio": 1.15}}
    t = ts.evaluate(et(1, 12, 30), uni, [], {})
    assert t and t.name == "volatility_shift"


def test_dynamic_build_gets_a_portfolio_review_every_twenty_minutes():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    ts.last_anchor_fired = loop.ANCHORS[-1]

    assert ts.evaluate(et(1, 12, 19), {"SPY": {"spot": 100.0}}, []) is None
    trigger = ts.evaluate(et(1, 12, 20), {"SPY": {"spot": 100.0}}, [])

    assert trigger and trigger.name == "portfolio_build_review"


def test_portfolio_build_continues_after_the_first_position():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    ts.last_anchor_fired = loop.ANCHORS[-1]

    trigger = ts.evaluate(
        et(1, 12, 31), {"SPY": {"spot": 100.0}}, [{"symbol": "SPY-option"}]
    )

    assert trigger and trigger.name == "portfolio_build_review"


def test_portfolio_build_ignores_arbitrary_four_but_stops_at_capacity_or_risk():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    ts.last_anchor_fired = loop.ANCHORS[-1]
    universe = {"SPY": {"spot": 100.0}}

    assert ts.evaluate(
        et(1, 12, 21), universe, [], structure_count=4,
        portfolio_risk_pct=0.02
    ).name == "portfolio_build_review"
    assert ts.evaluate(
        et(1, 12, 21), universe, [], structure_count=8,
        portfolio_risk_pct=0.02
    ) is None
    assert ts.evaluate(
        et(1, 12, 21), universe, [], structure_count=1,
        portfolio_risk_pct=0.04
    ) is None


def test_aggressive_profile_can_continue_build_reviews_above_balanced_target():
    ts = loop.TriggerState()
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    ts.last_anchor_fired = loop.ANCHORS[-1]
    universe = {"SPY": {"spot": 100.0}}

    trigger = ts.evaluate(
        et(1, 12, 21), universe, [], structure_count=2,
        portfolio_risk_pct=0.06, allocation_target_risk_pct=0.09)

    assert trigger and trigger.name == "portfolio_build_review"
    assert "9.0%" in trigger.detail


def test_cycle_cap_stops_escalation():
    ts = loop.TriggerState()
    ts.cycles_this_session = loop.MAX_CYCLES_PER_SESSION
    ts.record_cycle(et(1, 12, 0), {"SPY": {"spot": 100.0}})
    ts.last_anchor_fired = loop.ANCHORS[-1]
    assert ts.evaluate(et(1, 12, 30), {"SPY": {"spot": 110.0}}, [], {"SPY": 0.01}) is None


def test_cycle_cap_also_stops_urgent_scenario_review_but_latches_breach():
    ts = loop.TriggerState()
    ts.cycles_this_session = loop.MAX_CYCLES_PER_SESSION
    breached = {"portfolio_scenario_risk": {
        "status": "ok", "breached": True, "loss_dollars": 1600,
        "limit_dollars": 1500, "clear_below_dollars": 1400}}

    assert ts.evaluate(
        et(1, 12, 1), {}, [{}], portfolio_snapshot=breached) is None
    assert ts.scenario_breach_latched


def test_cycle_cap_also_stops_urgent_stop_review():
    ts = loop.TriggerState()
    ts.cycles_this_session = loop.MAX_CYCLES_PER_SESSION
    ts.portfolio_baseline = {"structures": {
        "sid-1": {"stop_progress": 0.40}}}
    current = {"structures": [{
        "structure_id": "sid-1", "stop_progress": 0.55}]}

    assert ts.evaluate(
        et(1, 12, 1), {}, [{}], portfolio_snapshot=current) is None


def test_closed_session_never_triggers():
    ts = loop.TriggerState()
    assert ts.evaluate(et(1, 6, 0), {"SPY": {"spot": 100.0}}, []) is None


def test_portfolio_scenario_first_crossing_bypasses_debounce_and_uses_hysteresis():
    ts = loop.TriggerState()
    ts.last_cycle_at = et(1, 12, 0)
    ts.last_anchor_fired = loop.ANCHORS[-1]
    breached = {"portfolio_scenario_risk": {
        "status": "ok", "breached": True, "loss_dollars": 1600,
        "limit_dollars": 1500, "clear_below_dollars": 1400}}

    first = ts.evaluate(et(1, 12, 1), {}, [{}], portfolio_snapshot=breached)
    assert first and first.name == "portfolio_scenario_breach"
    assert first.exempt_from_debounce
    assert ts.scenario_breach_latched
    assert ts.evaluate(et(1, 12, 2), {}, [{}], portfolio_snapshot=breached) is None

    cleared = {"portfolio_scenario_risk": {
        "status": "ok", "breached": False, "loss_dollars": 1390,
        "limit_dollars": 1500, "clear_below_dollars": 1400}}
    assert ts.evaluate(et(1, 12, 3), {}, [{}], portfolio_snapshot=cleared) is None
    assert not ts.scenario_breach_latched
    assert ts.evaluate(et(1, 12, 4), {}, [{}],
                       portfolio_snapshot=breached).name == "portfolio_scenario_breach"


def test_scenario_breach_latch_survives_runtime_state_roundtrip():
    ts = loop.TriggerState(scenario_breach_latched=True)
    assert loop.TriggerState.from_json(ts.to_json()).scenario_breach_latched is True


# --- exits -------------------------------------------------------------------

def test_profit_target_exit():
    pos = {"cost_basis": "1000", "unrealized_pl": "600"}
    due, why = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert due and "profit target" in why


def test_invalid_explicit_profit_policy_does_not_fall_back_to_entry_basis():
    pos = {"cost_basis": "1000", "unrealized_pl": "900",
           "executable_unrealized_pl": "900", "profit_target": 0,
           "profit_target_policy": {"validation_status": "invalid"}}
    due, why = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert not due and why == ""


def test_long_premium_has_no_drawdown_stop():
    """A stop here would sell the convexity the premium was bought to own."""
    pos = {"cost_basis": "1000", "unrealized_pl": "-900"}
    due, _ = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert not due


def test_short_premium_has_a_credit_multiple_stop():
    pos = {"cost_basis": "-500", "unrealized_pl": "-1100"}
    due, why = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert due and "short-premium stop" in why


def test_short_premium_stop_is_reachable_on_a_high_credit_capped_spread():
    pos = {"cost_basis": "-2532", "unrealized_pl": "-735",
           "premium_at_risk": 1468, "qty": 4}
    due, why = loop.position_exit_due(pos, {}, et(1, 12, 0), RP)
    assert due and "$734 loss threshold" in why


def test_thesis_time_stop_is_enforced_in_et():
    pos = {"cost_basis": "1000", "unrealized_pl": "0"}
    thesis = {"exit_at": "2026-09-01T15:30:00-04:00"}
    assert not loop.position_exit_due(pos, thesis, et(1, 15, 29), RP)[0]
    due, why = loop.position_exit_due(pos, thesis, et(1, 15, 30), RP)
    assert due and "thesis time stop" in why


def test_expiry_day_has_a_hard_stop_even_without_a_parseable_thesis():
    pos = {"cost_basis": "1000", "unrealized_pl": "0", "legs": [
        {"expiry": "2026-09-01"}, {"expiry": "2026-09-01"},
    ]}
    assert not loop.position_exit_due(pos, {"exit_time": "later"},
                                      et(1, 15, 14), RP)[0]
    due, why = loop.position_exit_due(pos, {"exit_time": "later"},
                                      et(1, 15, 15), RP)
    assert due and "expiry-day mandatory liquidation" in why


def test_currently_valid_settlement_authorization_suppresses_only_expiry_fallback():
    pos = {"cost_basis": "1000", "unrealized_pl": "0", "legs": [
        {"expiry": "2026-09-01"}, {"expiry": "2026-09-01"},
    ]}
    assert not loop.position_exit_due(
        pos, {}, et(1, 15, 20), RP, settlement_authorized=True)[0]
    due, why = loop.position_exit_due(
        pos, {"exit_at": "2026-09-01 15:18 ET"}, et(1, 15, 20), RP,
        settlement_authorized=True)
    assert due and "thesis time stop" in why


def test_settlement_authorization_also_suppresses_final_session_expiry_flatten():
    pos = {"cost_basis": "-100", "unrealized_pl": "0", "legs": [
        {"expiry": "2026-09-03"}, {"expiry": "2026-09-03"},
    ]}
    assert not loop.position_exit_due(
        pos, {}, et(3, 15, 10), RP, settlement_authorized=True)[0]


def test_final_session_time_stop():
    pos = {"cost_basis": "1000", "unrealized_pl": "0"}
    due, why = loop.position_exit_due(pos, {}, et(3, 15, 30), RP)
    assert due and "time stop" in why


def test_later_dated_option_is_not_liquidated_just_to_turn_equity_into_cash():
    pos = {"cost_basis": "1000", "unrealized_pl": "0", "legs": [
        {"expiry": "2026-09-11"}, {"expiry": "2026-09-11"},
    ]}

    due, why = loop.position_exit_due(pos, {}, et(3, 15, 30), RP)

    assert not due and why == ""


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
