import datetime as dt
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
    assert not RollingSeries().restore(tmp_path / "missing.json")
