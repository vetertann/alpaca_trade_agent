import datetime as dt
from collections import deque

import pytest

from agent.brain import preflight
from agent.brain.loop import Trigger, TriggerState
from agent.config import ET
from agent.run import Agent, _eligible_expiries
from agent.host.action_triggers import ActionTriggerStore
from agent.host.risk_params import DEFAULT as RP
from agent.host.thesis_store import ThesisStore


class Trace:
    def __init__(self):
        self.notes = []

    def note(self, message, **fields):
        self.notes.append((message, fields))

    def fill(self, state):
        pass

    def error(self, where, exc):
        raise AssertionError((where, exc))


class Ledger:
    def descriptors(self):
        return {}


def agent() -> Agent:
    value = Agent.__new__(Agent)
    value.trace = Trace()
    value.triggers = TriggerState()
    value.ledger = Ledger()
    value.startup_analysis_needed = False
    value._pending_triggers = deque(maxlen=32)
    value._pending_trigger_keys = set()
    value._last_position_quantities = None
    return value


def now(hour=12, minute=0):
    return dt.datetime(2026, 9, 1, hour, minute, tzinfo=ET)


def test_active_session_startup_fires_once():
    value = agent()
    value.startup_analysis_needed = True

    trigger = value._pop_startup_trigger("ACTIVE")

    assert trigger and trigger.name == "active_session_startup"
    assert trigger.exempt_from_debounce
    assert value._pop_startup_trigger("ACTIVE") is None


def test_action_trigger_risk_block_is_terminal_visible_and_urgent(tmp_path):
    value = agent()
    clock = dt.datetime.now(ET)
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    armed = value.action_triggers.set_exit(
        "sid-1", min_executable_profit=40, valid_for_seconds=60,
        reason="take profit", now=clock)
    trigger = value.action_triggers.current()[armed["trigger_id"]]

    value._handle_action_trigger_result(
        trigger, "entry",
        {"status": "blocked", "reason": "scenario cap",
         "failed_gates": ["portfolio_scenario"],
         "sizing": {"binding_constraint": "portfolio_scenario", "allowed_qty": 0}},
        clock)

    current = value.action_triggers.current()[armed["trigger_id"]]
    assert current["status"] == "blocked_risk"
    assert current["last_gate_failures"] == ["portfolio_scenario"]
    assert current["escalation_queued"] is True
    assert value.action_triggers.active(clock) == []
    visible = value.action_triggers.observable(clock)
    assert visible[0]["status"] == "blocked_risk"
    queued = value._pop_event_trigger(clock)
    assert queued and queued.name == "action_trigger_blocked"
    assert value.trace.notes[-1][0] == "action_trigger_blocked"


def test_action_trigger_transient_gate_failure_waits_with_backoff(tmp_path):
    value = agent()
    clock = dt.datetime.now(ET)
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    armed = value.action_triggers.set_exit(
        "sid-1", min_executable_profit=40, valid_for_seconds=60,
        reason="take profit", now=clock)
    trigger = value.action_triggers.current()[armed["trigger_id"]]

    value._handle_action_trigger_result(
        trigger, "entry",
        {"status": "blocked", "reason": "fresh quote is temporarily unusable",
         "failed_gates": ["quote_valid", "spread"]}, clock)

    current = value.action_triggers.current()[armed["trigger_id"]]
    assert current["status"] == "active"
    assert current["last_evaluation_status"] == "waiting_data"
    assert current["last_gate_failures"] == ["quote_valid", "spread"]
    assert value._pop_event_trigger(clock) is None
    assert value._trigger_data_retry_due(current, clock) is False
    assert value._trigger_data_retry_due(
        current, clock + dt.timedelta(seconds=6)) is True


def test_action_trigger_non_risk_block_fails_without_escalation(tmp_path):
    value = agent()
    clock = dt.datetime.now(ET)
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    armed = value.action_triggers.set_exit(
        "sid-1", min_executable_profit=40, valid_for_seconds=60,
        reason="take profit", now=clock)

    value._handle_action_trigger_result(
        value.action_triggers.current()[armed["trigger_id"]], "entry",
        {"status": "blocked", "reason": "submission state is unresolved",
         "failed_gates": ["execution_reconciliation"]}, clock)

    current = value.action_triggers.current()[armed["trigger_id"]]
    assert current["status"] == "failed"
    assert value._pop_event_trigger(clock) is None


def test_blocked_trigger_escalations_are_capped_at_three_per_session(tmp_path):
    value = agent()
    clock = dt.datetime.now(ET)
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    rows = []
    for index in range(4):
        armed = value.action_triggers.set_exit(
            f"sid-{index}", min_executable_profit=40, valid_for_seconds=60,
            reason="take profit", now=clock)
        trigger = value.action_triggers.current()[armed["trigger_id"]]
        value._handle_action_trigger_result(
            trigger, "entry",
            {"status": "blocked", "reason": "scenario cap",
             "failed_gates": ["portfolio_scenario"]}, clock)
        rows.append(value.action_triggers.current()[armed["trigger_id"]])

    assert [row["escalation_queued"] for row in rows] == [True, True, True, False]
    assert rows[-1]["escalation_suppressed_reason"] == \
        "blocked-trigger escalation cap reached"
    assert len(value._pending_triggers) == 3


def test_exempt_event_is_not_blocked_by_historical_cycle_count():
    value = agent()
    value.triggers.cycles_this_session = 999
    value._queue_trigger(
        Trigger("action_trigger_blocked", "risk review",
                exempt_from_debounce=True), key="blocked:1")

    trigger = value._pop_event_trigger(now())
    assert trigger and trigger.name == "action_trigger_blocked"


def test_action_trigger_price_miss_remains_active_and_labelled(tmp_path):
    value = agent()
    clock = dt.datetime.now(ET)
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    armed = value.action_triggers.set_exit(
        "sid-1", min_executable_profit=40, valid_for_seconds=60,
        reason="take profit", now=clock)
    trigger = value.action_triggers.current()[armed["trigger_id"]]

    value._handle_action_trigger_result(
        trigger, "exit",
        {"status": "condition_not_met", "executable_profit": 25,
         "reason": "fresh executable profit is below the authorized floor"},
        clock)

    current = value.action_triggers.current()[armed["trigger_id"]]
    assert current["status"] == "active"
    assert current["last_evaluation_status"] == "waiting_price"
    assert current["last_observed_value"] == 25


def test_spot_invalidation_requires_persistent_samples_then_arms_mandatory_exit(
        tmp_path):
    value = agent()
    value.trace.order = lambda *_args, **_kwargs: None
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    value.series = type("Series", (), {"last": lambda self, symbol: 699.0})()
    value.rest = object()

    class Executor:
        def __init__(self):
            self.calls = []

        def close_structure(self, structure, **kwargs):
            self.calls.append((structure, kwargs))
            return {"status": "submitted_close", "order_id": "close-1"}

    value.executor = Executor()
    value._latest_portfolio_snapshot = {"structures": [{
        "structure_id": "sid-spot", "underlying": "QQQ", "qty": 1,
        "legs": [],
    }]}
    clock = dt.datetime.now(ET)
    armed = value.action_triggers.set_exit(
        "sid-spot", spot_below=700, underlying="QQQ",
        confirmation_samples=2, sample_interval_seconds=10,
        valid_for_seconds=300, reason="level invalidates thesis", now=clock)

    assert value._evaluate_action_triggers(clock) == []
    current = value.action_triggers.current()[armed["trigger_id"]]
    assert current["status"] == "active" and current["consecutive_hits"] == 1
    assert value._evaluate_action_triggers(clock + dt.timedelta(seconds=5)) == []
    assert value.executor.calls == []

    result = value._evaluate_action_triggers(clock + dt.timedelta(seconds=11))
    assert result[0]["status"] == "submitted_close"
    assert value.executor.calls[0][1]["must_fill"] is True
    assert value.executor.calls[0][1]["mandatory_source"] == "spot_invalidation"
    assert value.action_triggers.current()[armed["trigger_id"]]["status"] == "fired"


def test_fired_entry_trigger_binds_broker_order_to_thesis(tmp_path):
    value = agent()
    value.trace.order = lambda *_args, **_kwargs: None
    value.theses = ThesisStore(tmp_path / "theses.jsonl")
    thesis = value.theses.open("entry", "QQQ", exit_profit="p",
                               exit_invalidation="i",
                               exit_time="2026-09-03 15:45 ET")
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    clock = dt.datetime.now(dt.timezone.utc)
    raw = value.action_triggers._append(
        "ACTION_TRIGGER", trigger_id="trigger-entry", purpose="entry",
        intent={"thesis_id": thesis.thesis_id}, condition={}, evidence={},
        reference_spot=700, max_spot_drift_pct=0.3, reason="entry",
        expires_at=(clock + dt.timedelta(seconds=60)).isoformat())
    trigger = value.action_triggers.current()[raw["trigger_id"]]

    value._handle_action_trigger_result(
        trigger, "entry", {"status": "submitted", "order_id": "order-1"}, clock)

    assert value.theses.get(thesis.thesis_id).order_ids == ["order-1"]


def test_entry_signal_needs_persistent_conflict_before_terminal_invalidation(
        tmp_path):
    value = agent()
    value.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")

    class Series:
        label = "bearish"

        def directional_contexts(self, symbols, _now=None):
            return {str(symbol).upper(): {"classification": self.label}
                    for symbol in symbols}

    value.series = Series()
    clock = dt.datetime.now(dt.timezone.utc)
    policy = {
        "schema_version": 1, "mode": "momentum_continuation",
        "candidate_bias": "bullish", "reference_spot": 100.0,
        "expected_move": 2.0, "max_adverse_move_em": 0.15,
        "confirmation_samples": 2, "sample_interval_seconds": 1.0,
    }
    raw = value.action_triggers._append(
        "ACTION_TRIGGER", trigger_id="signal-entry", purpose="entry",
        intent={"underlying": "SPY", "thesis_id": "thesis"},
        condition={"kind": "max_entry_debit", "value": 2.0},
        signal_policy=policy, signal_conflict_hits=0,
        reference_spot=100.0, max_spot_drift_pct=0.3,
        reason="momentum entry",
        expires_at=(clock + dt.timedelta(seconds=60)).isoformat())
    trigger = value.action_triggers.current()[raw["trigger_id"]]

    passed, first = value._sample_entry_signal(trigger, 99.9, clock)
    assert not passed and first["status"] == "conflicted"
    current = value.action_triggers.current()[raw["trigger_id"]]
    assert current["status"] == "active"
    assert current["signal_conflict_hits"] == 1
    assert current["last_evaluation_status"] == "waiting_signal"

    passed, second = value._sample_entry_signal(
        current, 99.8, clock + dt.timedelta(seconds=1))
    assert not passed and second["status"] == "conflicted"
    current = value.action_triggers.current()[raw["trigger_id"]]
    assert current["status"] == "invalidated_signal"
    assert current["signal_conflict_hits"] == 2
    assert value.action_triggers.active(clock + dt.timedelta(seconds=1)) == []
    assert value.trace.notes[-1][0] == "action_trigger_signal_invalidated"


def test_startup_waits_until_active():
    value = agent()
    value.startup_analysis_needed = True

    assert value._pop_startup_trigger("WARM_UP") is None
    assert value.startup_analysis_needed


def test_news_queues_a_debounced_decision_trigger():
    value = agent()

    value._on_news({"symbols": ["SPY"], "headline": "index-moving news"})
    trigger = value._pop_event_trigger(now())

    assert trigger and trigger.name == "relevant_news"
    assert not trigger.exempt_from_debounce


def test_irrelevant_news_does_not_queue_a_trigger():
    value = agent()

    value._on_news({"symbols": ["XYZ"], "headline": "irrelevant"})

    assert value._pop_event_trigger(now()) is None


def test_news_inside_debounce_is_suppressed_but_fill_is_immediate():
    value = agent()
    value.triggers.last_cycle_at = now(12, 0)
    value._on_news({"symbols": ["SPY"], "headline": "headline"})

    assert value._pop_event_trigger(now(12, 1)) is None
    assert value.trace.notes[-1][0] == "trigger_suppressed"

    value._on_trade_update({"event": "partial_fill", "order": {"id": "order-1"}})
    trigger = value._pop_event_trigger(now(12, 1))
    assert trigger and trigger.name == "fill_update"
    assert trigger.exempt_from_debounce


def test_assignment_is_detected_from_underlying_position_change():
    value = agent()
    value._detect_assignment([])

    value._detect_assignment([{"symbol": "SPY", "qty": "-100"}])
    trigger = value._pop_event_trigger(now())

    assert trigger and trigger.name == "assignment"
    assert "SPY qty -100" in trigger.detail


def test_expected_daily_move_is_annualized_iv_over_sqrt_252():
    moves = Agent._expected_daily_moves({
        "SPY": {"iv_atm": 0.1587450787},
        "QQQ": {"iv_atm": None},
    })

    assert moves["SPY"] == pytest.approx(0.01)
    assert "QQQ" not in moves


def test_expiry_discovery_keeps_every_future_broker_listing():
    rows = [{"expiration_date": value} for value in (
        "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03",
        "2026-09-04", "2026-09-08", "2026-09-11", "2026-09-14")]

    assert _eligible_expiries(rows, dt.date(2026, 8, 31)) == [
        "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03",
        "2026-09-04", "2026-09-08", "2026-09-11", "2026-09-14"]


def test_stop_approach_bypasses_debounce_but_deterministic_stop_remains_separate():
    state = TriggerState()
    baseline = {"equity": 100_000, "structures": [{
        "structure_id": "sid-1", "broker_unrealized_pl": -50,
        "stop_progress": 0.40, "pnl_if_expired_now_per_unit": 25}]}
    state.record_cycle(now(12, 0), {}, portfolio_snapshot=baseline)
    current = {"equity": 99_950, "structures": [{
        "structure_id": "sid-1", "broker_unrealized_pl": -90,
        "stop_progress": 0.55, "pnl_if_expired_now_per_unit": 5}]}

    trigger = state.evaluate(now(12, 1), {}, [{}], portfolio_snapshot=current)

    assert trigger and trigger.name == "stop_approach"
    assert trigger.exempt_from_debounce


def test_equity_and_structure_marks_can_trigger_a_post_debounce_review():
    state = TriggerState()
    state.record_cycle(now(12, 0), {}, portfolio_snapshot={
        "equity": 100_000, "structures": [{
            "structure_id": "sid-1", "broker_unrealized_pl": -20,
            "stop_progress": 0.10, "pnl_if_expired_now_per_unit": 20}]})

    trigger = state.evaluate(now(12, 11), {}, [{}], portfolio_snapshot={
        "equity": 99_800, "structures": [{
            "structure_id": "sid-1", "broker_unrealized_pl": -120,
            "stop_progress": 0.30, "pnl_if_expired_now_per_unit": -10}]})

    assert trigger and trigger.name == "portfolio_deterioration"


def test_live_trigger_universe_uses_current_stream_not_previous_spot(monkeypatch):
    class Series:
        def last(self, symbol): return {"SPY": 102.0}.get(symbol)
        def session_range(self, symbol): return (99.0, 103.0) if symbol == "SPY" else None
        def realized_vol(self, symbol): return 0.20 if symbol == "SPY" else None

    class Rest:
        def stock_latest_trade(self, symbol): return {"p": 50}

    monkeypatch.setattr(preflight, "_atm_iv", lambda *args: (0.30, "2026-09-01"))
    got = preflight.live_trigger_universe(
        Rest(), Series(), {"SPY": {"spot": 100.0, "realized_vol": 0.15,
                                   "realized_vol_by_window": {"rv5": 0.10}}},
        ["2026-09-01"], now(), refresh_iv=True)

    assert got["SPY"]["spot"] == 102.0
    assert got["SPY"]["iv_atm"] == 0.30
    assert got["SPY"]["iv_rv_ratio"] == 2.0
    assert got["SPY"]["iv_intraday_rv_ratio"] == 1.5


def test_continuous_snapshot_carries_host_scenario_state():
    class Rest:
        def option_quotes(self, symbols): return {}

    class Series:
        def last(self, symbol): return None

    class Theses:
        def list(self, status): return []

    value = Agent.__new__(Agent)
    value.rest, value.series, value.theses = Rest(), Series(), Theses()
    value.params, value.trace = RP, Trace()
    value.exit_policies = None
    value.starting_equity = 100_000

    snapshot = value._sample_portfolio(
        now(), account={"equity": "100000"}, positions=[],
        risk_state={"structures": [], "premium_at_risk": 0, "realised_loss": 0},
        record=False, trace_record=False)

    assert snapshot["portfolio_scenario_risk"] == {
        **snapshot["portfolio_scenario_risk"], "status": "ok", "breached": False}
    assert snapshot["portfolio_scenario_risk"]["loss_dollars"] == 0
    assert snapshot["portfolio_scenario_risk"]["limit_dollars"] == 4000
