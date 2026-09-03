import datetime as dt

import pytest

from agent.host import entry_signal
from agent.host.action_triggers import ActionTriggerStore, entry_condition
from agent.types import Leg, TradeIntent


NOW = dt.datetime(2026, 9, 3, 15, 0, tzinfo=dt.timezone.utc)


def evidence(*, bias="bullish", directionality="direction-led",
             label="bullish"):
    return {"direction": {
        "candidate_bias": bias,
        "directionality": directionality,
        "spot": 100.0,
        "expected_move": 2.0,
        "market_direction": {"classification": label},
    }}


def trade():
    expiry = dt.date(2026, 9, 4)
    return TradeIntent(
        "SPY", "vertical_call", (
            Leg("SPY260904C00100000", 1, "buy", "buy_to_open",
                100, "call", expiry),
            Leg("SPY260904C00105000", 1, "sell", "sell_to_open",
                105, "call", expiry)),
        "thesis", 1000)


def test_auto_policy_is_host_derived_and_direction_agnostic_for_volatility():
    directional = entry_signal.build_policy(evidence(), entry_mode="auto")
    volatility = entry_signal.build_policy(
        evidence(bias="neutral", directionality="volatility-led"),
        entry_mode="auto")

    assert directional["mode"] == "momentum_continuation"
    assert directional["candidate_bias"] == "bullish"
    assert volatility["mode"] == "direction_agnostic"


def test_momentum_requires_current_alignment_and_flags_confirmable_conflict():
    policy = entry_signal.build_policy(evidence(), entry_mode="momentum_continuation")

    assert entry_signal.evaluate(
        policy, {"classification": "bullish"}, 100.1)["status"] == "passed"
    neutral = entry_signal.evaluate(policy, {"classification": "neutral"}, 100.0)
    assert neutral["status"] == "waiting_signal" and not neutral["conflict"]
    opposite = entry_signal.evaluate(policy, {"classification": "bearish"}, 99.9)
    assert opposite["status"] == "conflicted" and opposite["conflict"]


def test_pullback_tolerates_small_dip_but_not_confirmed_reversal_geometry():
    policy = entry_signal.build_policy(
        evidence(), entry_mode="pullback_entry", max_adverse_move_em=0.15)

    small = entry_signal.evaluate(policy, {"classification": "neutral"}, 99.8)
    assert small["status"] == "passed"
    unstable = entry_signal.evaluate(policy, {"classification": "bearish"}, 99.8)
    assert unstable["status"] == "waiting_signal"
    reversal = entry_signal.evaluate(policy, {"classification": "bearish"}, 99.6)
    assert reversal["status"] == "conflicted"


def test_directional_policy_refuses_missing_host_direction_evidence():
    with pytest.raises(ValueError, match="host-computed bullish or bearish"):
        entry_signal.build_policy({}, entry_mode="momentum_continuation")


def test_direction_led_candidate_cannot_disable_signal_gate():
    with pytest.raises(ValueError, match="direction_agnostic is allowed only"):
        entry_signal.build_policy(evidence(), entry_mode="direction_agnostic")


def test_signal_policy_is_durable_and_part_of_trigger_identity(tmp_path):
    store = ActionTriggerStore(tmp_path / "triggers.jsonl")
    momentum = entry_signal.build_policy(evidence(), entry_mode="momentum_continuation")
    pullback = entry_signal.build_policy(evidence(), entry_mode="pullback_entry")
    kwargs = dict(
        intent=trade(), condition=entry_condition(max_entry_debit=2.0),
        valid_for_seconds=60, reference_spot=100.0,
        max_spot_drift_pct=0.3, evidence=evidence(),
        reason="entry at reviewed price", now=NOW)

    first = store.set_entry(signal_policy=momentum, **kwargs)
    duplicate = store.set_entry(signal_policy=momentum, **kwargs)
    second = store.set_entry(signal_policy=pullback, **kwargs)

    assert duplicate["trigger_id"] == first["trigger_id"]
    assert second["trigger_id"] != first["trigger_id"]
    restored = ActionTriggerStore(store.path).current()[first["trigger_id"]]
    assert restored["signal_policy"] == momentum
