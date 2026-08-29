"""What the agent remembers between cycles."""
import datetime as dt
from agent.host.thesis_store import ThesisStore
from agent.brain import preflight


def store(tmp_path):
    return ThesisStore(tmp_path / "t.jsonl")


def test_closed_thesis_records_how_it_ended(tmp_path):
    s = store(tmp_path)
    t = s.open("IV cheap vs realized", "SPY", exit_profit="50%",
               exit_invalidation="iv/rv > 1.0", exit_time="Thu 16:00")
    s.close(t.thesis_id, reason="time stop, final session", realised=-180.0)
    out = s.outcomes()
    assert len(out) == 1
    assert "time stop" in out[0]["ended"] and "180" in out[0]["ended"]
    assert s.list("open") == []


def test_outcomes_are_most_recent_first(tmp_path):
    s = store(tmp_path)
    ids = []
    for i in range(3):
        t = s.open(f"h{i}", "SPY", exit_profit="p", exit_invalidation="i", exit_time="t")
        object.__setattr__(t, "opened_at",
                           dt.datetime(2026, 9, 1, 10 + i, tzinfo=dt.timezone.utc))
        s.close(t.thesis_id, reason=f"reason {i}")
        ids.append(t.thesis_id)
    assert [o["thesis_id"] for o in s.outcomes()] == list(reversed(ids))


def test_outcomes_respects_the_limit(tmp_path):
    s = store(tmp_path)
    for i in range(9):
        t = s.open(f"h{i}", "SPY", exit_profit="p", exit_invalidation="i", exit_time="t")
        s.close(t.thesis_id, reason="r")
    assert len(s.outcomes(4)) == 4


def test_open_theses_are_not_in_outcomes(tmp_path):
    s = store(tmp_path)
    s.open("still running", "SPY", exit_profit="p", exit_invalidation="i", exit_time="t")
    assert s.outcomes() == []


class FakeRest:
    def positions(self): return []
    def stock_latest_trade(self, s): return {"p": 100.0}
    def contracts(self, *a, **k): return []
    def option_quotes(self, syms): return {}
    def stock_bars(self, *a, **k): return []


class FakeSeries:
    def last(self, s): return 100.0
    def realized_vol(self, s): return 0.12
    def session_range(self, s): return (99.0, 101.0)


def build(tmp_path, **kw):
    return preflight.build(
        FakeRest(), FakeSeries(), store(tmp_path),
        trigger={"name": "anchor"}, universe=["SPY"], expiries=["2026-09-03"],
        account={"equity": "100000", "cash": "100000", "options_buying_power": "100000",
                 "options_trading_level": 3},
        **kw)


def test_bundle_carries_the_decision_record(tmp_path):
    hist = [{"at": "2026-09-01T11:00", "trigger": "anchor",
             "outcome": "NO_TRADE", "reason": "spread gate"}]
    blocked = [{"at": "2026-09-01T11:00", "structure": "SPY vertical_call 770c/775c",
                "failed": ["spread"]}]
    b = build(tmp_path, history=hist, blocked=blocked)
    assert b["recent_cycles"] == hist
    assert b["blocked_structures"] == blocked
    assert b["closed_theses"] == []


def test_bundle_history_is_bounded(tmp_path):
    hist = [{"at": str(i), "trigger": "t", "outcome": "NO_TRADE", "reason": "r"}
            for i in range(30)]
    b = build(tmp_path, history=hist)
    assert len(b["recent_cycles"]) == 8
    assert b["recent_cycles"][-1]["at"] == "29", "must keep the newest, not the oldest"


def test_bundle_without_history_is_still_valid(tmp_path):
    b = build(tmp_path)
    assert b["recent_cycles"] == [] and b["blocked_structures"] == []
    spy = b["universe"]["SPY"]
    assert spy["realized_vol_source"] == "intraday"
    assert spy["realized_vol"] == spy["intraday_realized_vol"] == 0.12
    assert spy["realized_vol_by_window"] == {}


def test_daily_windows_remain_available_when_intraday_stream_is_warm(tmp_path,
                                                                     monkeypatch):
    class DailyRest(FakeRest):
        def stock_bars(self, *args, **kwargs):
            closes = []
            price = 100.0
            returns = (0.012, -0.006, 0.004, -0.009, 0.007)
            for i in range(70):
                price *= 1 + returns[i % len(returns)]
                closes.append({"c": price})
            return closes

    monkeypatch.setattr(preflight, "_atm_iv",
                        lambda *args, **kwargs: (0.20, "2026-09-03"))
    b = preflight.build(
        DailyRest(), FakeSeries(), store(tmp_path),
        trigger={"name": "anchor"}, universe=["SPY"], expiries=["2026-09-03"],
        account={"equity": "100000", "cash": "100000",
                 "options_buying_power": "100000", "options_trading_level": 3},
    )

    spy = b["universe"]["SPY"]
    assert set(spy["realized_vol_by_window"]) == {"rv5", "rv10", "rv20", "rv60"}
    assert spy["realized_vol_source"] == "daily_ewma"
    assert spy["intraday_realized_vol"] == 0.12
    assert spy["realized_vol"] != spy["intraday_realized_vol"]
    assert spy["iv_rv_by_window"]["rv5"] == round(
        spy["iv_atm"] / spy["realized_vol_by_window"]["rv5"], 3)
