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


def test_dated_exit_time_is_normalized_to_et_and_survives_restart(tmp_path):
    s = make(tmp_path)
    t = s.open("h", "SPY", exit_profit="p", exit_invalidation="i",
               exit_time="2026-09-01 15:45 ET")
    assert t.exit_at == "2026-09-01T15:45:00-04:00"
    assert make(tmp_path).get(t.thesis_id).exit_at == t.exit_at


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


def test_host_exit_policy_binds_once_and_survives_restart(tmp_path):
    s = make(tmp_path)
    t = s.open("h", "SPY", exit_profit="p", exit_invalidation="i",
               exit_time="2026-09-03 15:45 ET")
    policy = {"schema_version": 1, "premium_type": "short",
              "loss_stops": [{"kind": "max_loss_fraction", "value": 0.5}]}

    s.bind_exit_policy(t.thesis_id, policy)
    size_after_first_bind = (tmp_path / "theses.jsonl").stat().st_size
    s.bind_exit_policy(t.thesis_id, policy)

    assert (tmp_path / "theses.jsonl").stat().st_size == size_after_first_bind
    assert make(tmp_path).get(t.thesis_id).enforced_exit_policy == policy
