import datetime as dt
import json
from types import SimpleNamespace

from agent.brain.loop import TriggerState
from agent.config import ET
from agent.run import Agent


def et(day, hour, minute):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=ET)


def bare_agent(tmp_path):
    agent = Agent.__new__(Agent)
    agent.run_dir = str(tmp_path)
    agent.triggers = TriggerState()
    agent.previous_bundle = None
    agent.history = []
    agent.blocked = []
    agent.starting_equity = None
    agent.starting_equity_captured_at = None
    agent.restart_rebaseline_needed = False
    return agent


def test_fresh_same_session_runtime_state_roundtrips_without_staged_drafts(tmp_path):
    first = bare_agent(tmp_path)
    first.starting_equity = 100_000
    first.starting_equity_captured_at = "2026-09-01T13:30:00+00:00"
    first.previous_bundle = {"bundle_hash": "abc", "universe": {"SPY": {"spot": 100}}}
    first.history = [{"outcome": "NO_TRADE"}]
    first.blocked = [{"structure": "SPY vertical"}]
    first.triggers.record_cycle(et(1, 11, 0), first.previous_bundle["universe"])
    first.executor = SimpleNamespace(_staged={"must_not_survive": object()})
    first._checkpoint_runtime_state(et(1, 11, 1))

    raw = json.loads((tmp_path / "runtime_state.json").read_text())
    assert "staged" not in json.dumps(raw).lower()

    restarted = bare_agent(tmp_path)
    assert restarted._restore_runtime_state(et(1, 11, 3))
    assert restarted.starting_equity == 100_000
    assert restarted.previous_bundle["bundle_hash"] == "abc"
    assert restarted.triggers.baseline["SPY"]["spot"] == 100
    assert not restarted.restart_rebaseline_needed


def test_stale_same_session_state_discards_diff_and_reconstructs_anchor(tmp_path):
    first = bare_agent(tmp_path)
    first.previous_bundle = {"universe": {"SPY": {"spot": 100}}}
    first.triggers.record_cycle(et(1, 9, 46), first.previous_bundle["universe"])
    first._checkpoint_runtime_state(et(1, 9, 47))

    restarted = bare_agent(tmp_path)
    restarted._restore_runtime_state(et(1, 11, 30))
    assert restarted.previous_bundle is None
    assert restarted.triggers.baseline == {}
    assert restarted.triggers.last_anchor_fired == dt.time(11, 0)
    assert restarted.restart_rebaseline_needed


def test_new_session_keeps_campaign_baseline_but_drops_intraday_state(tmp_path):
    first = bare_agent(tmp_path)
    first.starting_equity = 100_000
    first.previous_bundle = {"universe": {"SPY": {"spot": 100}}}
    first.triggers.record_cycle(et(1, 14, 0), first.previous_bundle["universe"])
    first._checkpoint_runtime_state(et(1, 14, 1))

    restarted = bare_agent(tmp_path)
    restarted._restore_runtime_state(et(2, 9, 0))
    assert restarted.starting_equity == 100_000
    assert restarted.previous_bundle is None
    assert restarted.triggers.cycles_this_session == 0
    assert restarted.triggers.baseline == {}


def test_prompt_policy_change_drops_old_decision_summaries_only(tmp_path):
    first = bare_agent(tmp_path)
    first.history = [{"outcome": "NO_TRADE", "reason": "old expiry cutoff"}]
    first.blocked = [{"structure": "still a real gate record"}]
    first._checkpoint_runtime_state(et(1, 11, 0))
    raw_path = tmp_path / "runtime_state.json"
    raw = json.loads(raw_path.read_text())
    raw["prompt_policy_version"] = "obsolete-policy"
    raw_path.write_text(json.dumps(raw))

    restarted = bare_agent(tmp_path)
    assert restarted._restore_runtime_state(et(1, 11, 1))
    assert restarted.history == []
    assert restarted.blocked == [{"structure": "still a real gate record"}]
