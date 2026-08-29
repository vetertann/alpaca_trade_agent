from agent.host.thesis_store import ThesisStore


def make(tmp_path):
    return ThesisStore(tmp_path / "theses.jsonl")


def test_open_and_list(tmp_path):
    s = make(tmp_path)
    t = s.open("IV cheap vs realized", "SPY", exit_profit="50% of max",
               exit_invalidation="IV/RV back above 1.0", exit_time="Thu 16:00 ET",
               exit_news="Fed signals a cut")
    assert t.thesis_id.startswith("th_") and "spy" in t.thesis_id
    assert len(s.list("open")) == 1


def test_update_and_close(tmp_path):
    s = make(tmp_path)
    t = s.open("h", "QQQ", exit_profit="p", exit_invalidation="i", exit_time="t")
    s.update(t.thesis_id, note="filled", order_ids=["ord-1"], gates={"economics": "YES"})
    s.update(t.thesis_id, status="closed")
    assert s.get(t.thesis_id).status == "closed"
    assert s.get(t.thesis_id).order_ids == ["ord-1"]
    assert s.list("open") == []


def test_survives_restart(tmp_path):
    s = make(tmp_path)
    t = s.open("h", "SPY", exit_profit="p", exit_invalidation="i", exit_time="t")
    s.update(t.thesis_id, note="one")
    s2 = ThesisStore(tmp_path / "theses.jsonl")
    got = s2.get(t.thesis_id)
    assert got is not None and got.hypothesis == "h" and got.notes


def test_open_exposure_check(tmp_path):
    s = make(tmp_path)
    s.open("h", "SPY", exit_profit="p", exit_invalidation="i", exit_time="t")
    assert s.has_open_exposure("SPY")
    assert not s.has_open_exposure("IWM")
