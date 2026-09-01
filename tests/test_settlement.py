import datetime as dt

from agent.config import ET
from agent.host.settlement import SettlementAuthorizationStore
from agent.run import _settlement_status


def structure():
    return {
        "structure_id": "sid", "premium_at_risk": 500,
        "spot": 100, "missing_exit_quotes": [],
        "legs": [
            {"expiry": "2026-09-01", "side": "sell", "strike": 95,
             "option_type": "put"},
            {"expiry": "2026-09-01", "side": "buy", "strike": 90,
             "option_type": "put"},
        ],
    }


def test_store_and_continuous_live_validation(tmp_path):
    store = SettlementAuthorizationStore(tmp_path / "settlement.jsonl")
    auth = store.authorize("sid", min_short_distance_points=3,
                           reason="accept defined-risk settlement")
    now = dt.datetime(2026, 9, 1, 15, 20, tzinfo=ET)
    safe = _settlement_status(
        structure(), auth, now=now, scenario={"status": "ok", "breached": False},
        options_buying_power=1000)
    assert safe["authorized"] is True

    moved = {**structure(), "spot": 97}
    unsafe = _settlement_status(
        moved, auth, now=now, scenario={"status": "ok", "breached": False},
        options_buying_power=1000)
    assert unsafe["authorized"] is False
    assert "short strike" in unsafe["reason"]
    assert SettlementAuthorizationStore(store.path).active("sid") is not None


def test_missing_quotes_or_scenario_breach_revokes_effective_authorization(tmp_path):
    store = SettlementAuthorizationStore(tmp_path / "settlement.jsonl")
    auth = store.authorize("sid", min_short_distance_points=3, reason="settle")
    now = dt.datetime(2026, 9, 1, 15, 20, tzinfo=ET)
    missing = {**structure(), "missing_exit_quotes": ["leg"]}
    assert not _settlement_status(
        missing, auth, now=now, scenario={"status": "ok", "breached": False},
        options_buying_power=1000)["authorized"]
    assert not _settlement_status(
        structure(), auth, now=now, scenario={"status": "ok", "breached": True},
        options_buying_power=1000)["authorized"]
