import datetime as dt

from agent.brain import scheduled_events
from agent.config import ET


def test_event_context_is_labelled_advisory_and_et_relative():
    now = dt.datetime(2026, 9, 2, 9, 30, tzinfo=ET)
    out = scheduled_events.context(now)

    assert out["label"] == "scheduled_event_context"
    assert out["advisory_only"] is True
    assert out["not_a_blackout_gate"] is True
    assert out["outcomes_not_included"] is True
    assert out["next_event"]["name"].startswith("Manufacturers'")
    assert out["next_event"]["minutes_until"] == 30.0


def test_event_context_keeps_recent_release_distinct_from_upcoming():
    now = dt.datetime(2026, 9, 1, 10, 15, tzinfo=ET)
    rows = scheduled_events.context(now)["events"]

    recent = [row for row in rows if row["timing"] == "recently_released"]
    assert {row["market_relevance"] for row in recent} >= {
        "labour_market", "broad_macro"}
    assert all(row["minutes_until"] < 0 for row in recent)
