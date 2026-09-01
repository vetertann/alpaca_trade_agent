import datetime as dt
import threading
from collections import deque

from agent.host.series import RollingSeries

T0 = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.timezone.utc)


def feed(rs, prices, step=60):
    for i, p in enumerate(prices):
        rs.observe("SPY", p, T0 + dt.timedelta(seconds=i * step))


def test_last_and_range():
    rs = RollingSeries()
    feed(rs, [100, 102, 99, 101])
    assert rs.last("SPY") == 101
    assert rs.session_range("SPY") == (99, 102)


def test_move_since():
    rs = RollingSeries()
    feed(rs, [100, 105])
    assert abs(rs.move_since("SPY", T0) - 0.05) < 1e-9


def test_realized_vol_needs_enough_points():
    rs = RollingSeries()
    feed(rs, [100, 101])
    assert rs.realized_vol("SPY") is None
    feed(rs, [100 + (i % 3) for i in range(40)])
    v = rs.realized_vol("SPY")
    assert v is not None and v > 0


def test_directional_context_labels_persistent_move_and_explains_fields():
    rs = RollingSeries()
    feed(rs, [100 + i * 0.05 for i in range(70)])

    out = rs.directional_context("SPY")

    assert out["classification"] == "bullish"
    assert out["strength"] == "strong"
    assert out["return_15m"] > 0
    assert out["normalized_move_15m"] > 0
    assert out["trend_efficiency_15m"] == 1.0
    assert out["session_range_position"] == 1.0
    assert out["source"] == "streamed equity quote midpoints"
    assert out["sample_coverage_minutes"] >= 60
    assert out["return_since_first_stream_observation"] > 0
    assert "return_since_observed_session_open" not in out
    assert any("positive normalized" in item
               for item in out["classification_basis"])


def test_directional_context_is_insufficient_until_history_exists():
    rs = RollingSeries()
    feed(rs, [100, 100.1, 100.2])

    out = rs.directional_context("SPY")

    assert out["classification"] == "insufficient_data"
    assert out["return_15m"] is None
    assert out["sample_coverage_minutes"] == 2.0


def test_directional_contexts_names_cross_asset_confirmation():
    rs = RollingSeries()
    for symbol, scale in (("SPY", 0.05), ("QQQ", 0.07), ("IWM", 0.03)):
        for i in range(70):
            rs.observe(symbol, 100 + i * scale, T0 + dt.timedelta(minutes=i))

    out = rs.directional_contexts({"SPY", "QQQ", "IWM"})["SPY"]

    confirmation = out["cross_asset_confirmation"]
    assert confirmation["same_direction_count_including_self"] == 3
    assert confirmation["peers"]["QQQ"]["classification"] == "bullish"
    assert "3 observed underlyings" in confirmation["interpretation"]


def test_restart_proof_gap_reference_downgrades_gap_without_continuation():
    rs = RollingSeries()
    for i in range(70):
        rs.observe("SPY", 102 + i * .01, T0 + dt.timedelta(minutes=i))
    rs.set_session_reference(
        "SPY", dt.date(2026, 9, 1), prior_close=100, session_open=102,
        expected_move=4, source="completed daily bar plus official 09:30 bar open")

    out = rs.directional_context("SPY")

    assert out["session_reference"]["available"] is True
    assert out["session_reference"]["gap_move_em"] == .5
    assert out["session_reference"]["intraday_move_em"] < .25
    assert out["classification"] == "neutral"
    assert any("gap dominated" in reason for reason in out["classification_basis"])


def test_missing_opening_bar_is_explicitly_unavailable_not_zero():
    rs = RollingSeries()
    feed(rs, [100 + i * .05 for i in range(70)])
    reference = rs.directional_context("SPY")["session_reference"]
    assert reference["available"] is False
    assert reference["gap_move_em"] is None


def test_ignores_nonpositive_prices():
    rs = RollingSeries()
    feed(rs, [0, -1, 100])
    assert rs.last("SPY") == 100


def test_checkpoint_roundtrip(tmp_path):
    rs = RollingSeries()
    feed(rs, list(range(100, 130)))
    p = tmp_path / "series.json"
    rs.checkpoint(p)
    rs2 = RollingSeries()
    assert rs2.restore(p)
    assert rs2.minute_closes("SPY") == rs.minute_closes("SPY")
    assert rs2.last("SPY") == rs.last("SPY")
    assert rs2.session_range("SPY") == rs.session_range("SPY")
    assert not RollingSeries().restore(tmp_path / "missing.json")


def test_stream_append_cannot_mutate_deque_during_preflight_read():
    started = threading.Event()
    release = threading.Event()
    writer_attempted = threading.Event()
    writer_done = threading.Event()

    class PausingDeque(deque):
        def __iter__(self):
            iterator = super().__iter__()
            for index, item in enumerate(iterator):
                if index == 0:
                    started.set()
                    assert release.wait(1)
                yield item

    rs = RollingSeries()
    rs.sec["SPY"] = PausingDeque([(T0, 100.0), (T0, 101.0)], maxlen=10)
    errors = []

    def read():
        try:
            rs.session_range("SPY")
        except Exception as exc:  # pragma: no cover - assertion reports the value
            errors.append(exc)

    def write():
        writer_attempted.set()
        rs.observe("SPY", 102.0, T0)
        writer_done.set()

    reader = threading.Thread(target=read)
    writer = threading.Thread(target=write)
    reader.start()
    assert started.wait(1)
    writer.start()
    assert writer_attempted.wait(1)
    assert not writer_done.wait(0.05)  # blocked on the read snapshot lock
    release.set()
    reader.join(1)
    writer.join(1)
    assert not errors
    assert writer_done.is_set()
