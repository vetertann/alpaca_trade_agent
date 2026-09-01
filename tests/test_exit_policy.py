import datetime as dt
from types import SimpleNamespace

import pytest

from agent.brain.loop import position_exit_due
from agent.config import ET
from agent.host.exit_policy import ExitPolicyStore
from agent.host.risk_params import DEFAULT
from agent.run import Agent


def policy(store, sid="sid-1"):
    return store.set(
        sid, activation_profit=30, max_profit_giveback=10,
        minimum_locked_profit=15, confirmation_samples=2,
        hard_profit_target=100, reason="protect a meaningful executable gain")


def test_policy_arms_trails_confirms_and_survives_restart(tmp_path):
    path = tmp_path / "exit_policies.jsonl"
    store = ExitPolicyStore(path)
    policy(store)

    assert not store.observe("sid-1", 20, quotes_valid=True)["armed"]
    armed = store.observe("sid-1", 35, quotes_valid=True)
    assert armed["armed"] and armed["current_trigger_profit"] == 25
    assert not store.observe("sid-1", 24, quotes_valid=True)["triggered"]
    fired = store.observe("sid-1", 23, quotes_valid=True)
    assert fired["triggered"] and fired["breach_count"] == 2

    restored = ExitPolicyStore(path).view("sid-1")
    assert restored["high_water_profit"] == 35
    assert restored["breach_count"] == 2


def test_policy_updates_can_only_tighten(tmp_path):
    store = ExitPolicyStore(tmp_path / "exit_policies.jsonl")
    policy(store)
    with pytest.raises(ValueError, match="lowered"):
        store.set("sid-1", activation_profit=40, max_profit_giveback=10,
                  minimum_locked_profit=15, confirmation_samples=2,
                  hard_profit_target=100, reason="relax")

    tightened = store.set(
        "sid-1", activation_profit=25, max_profit_giveback=8,
        minimum_locked_profit=16, confirmation_samples=1,
        hard_profit_target=100, reason="lock more")
    assert tightened["activation_profit"] == 25
    assert tightened["minimum_locked_profit"] == 16


def test_invalid_quotes_never_advance_or_fire_policy(tmp_path):
    store = ExitPolicyStore(tmp_path / "exit_policies.jsonl")
    policy(store)
    row = store.observe("sid-1", 50, quotes_valid=False)
    assert not row["observation_valid"] and not row["armed"] and not row["triggered"]


def test_profit_target_requires_executable_value_when_requested():
    position = {"cost_basis": 100, "unrealized_pl": 100,
                "broker_unrealized_pl": 100, "executable_unrealized_pl": 20,
                "legs": []}
    due, _ = position_exit_due(
        position, None, dt.datetime(2026, 9, 1, 12, tzinfo=ET), DEFAULT,
        require_executable_profit=True)
    assert not due
    position["executable_unrealized_pl"] = 60
    due, why = position_exit_due(
        position, None, dt.datetime(2026, 9, 1, 12, tzinfo=ET), DEFAULT,
        require_executable_profit=True)
    assert due and "executable profit" in why


def test_tier_zero_adaptive_exit_submits_without_a_model_turn(tmp_path):
    sid = "sid-1"
    store = ExitPolicyStore(tmp_path / "exit_policies.jsonl")
    policy(store, sid)
    closed = []

    class Executor:
        def close_structure(self, structure, reason, now):
            closed.append((structure["structure_id"], reason))
            return {"status": "submitted_close", "structure_id": sid}

    class Trace:
        def note(self, *args, **kwargs): pass
        def order(self, *args, **kwargs): pass
        def error(self, where, exc): raise AssertionError((where, exc))

    agent = Agent.__new__(Agent)
    agent.exit_policies = store
    agent.executor = Executor()
    agent.theses = SimpleNamespace(get=lambda thesis_id: None)
    agent.params = DEFAULT
    agent.trace = Trace()

    def snapshot(pnl):
        return {"structures": [{
            "structure_id": sid, "thesis_id": "", "family": "vertical_put",
            "underlying": "SPY", "qty": 1, "cost_basis": 200,
            "broker_unrealized_pl": pnl, "executable_unrealized_pl": pnl,
            "premium_at_risk": 200, "missing_exit_quotes": [], "legs": [],
        }]}

    now = dt.datetime(2026, 9, 1, 12, tzinfo=ET)
    assert not agent._evaluate_snapshot_exits(snapshot(35), now,
                                              observe_adaptive=True)
    assert not agent._evaluate_snapshot_exits(snapshot(24), now,
                                              observe_adaptive=True)
    acted = agent._evaluate_snapshot_exits(snapshot(23), now,
                                           observe_adaptive=True)
    assert acted and closed[0][0] == sid
    assert "adaptive executable-profit trail" in closed[0][1]
