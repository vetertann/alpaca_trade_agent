import math
from agent.quant import vol


def bars(closes):
    return [{"c": c} for c in closes]


def test_realized_needs_enough_points():
    assert vol.realized_from_bars(bars([100, 101])) is None


def test_flat_series_has_zero_vol():
    assert vol.realized_from_bars(bars([100] * 30)) == 0.0


def test_higher_dispersion_gives_higher_vol():
    calm = vol.realized_from_bars(bars([100 + (i % 2) * 0.5 for i in range(30)]))
    wild = vol.realized_from_bars(bars([100 + (i % 2) * 5.0 for i in range(30)]))
    assert wild > calm > 0


def test_ewma_reacts_to_recent_moves():
    quiet_then_wild = [100.0] * 40 + [100 + (i % 2) * 8.0 for i in range(20)]
    e = vol.ewma_from_bars(bars(quiet_then_wild))
    flat = vol.realized_from_bars(bars(quiet_then_wild), window=59)
    assert e > flat


def test_blend_prefers_intraday_and_names_source():
    assert vol.blend(0.12, 0.20) == (0.12, "intraday")
    assert vol.blend(None, 0.20) == (0.20, "daily_bars")
    assert vol.blend(None, None) == (None, "unavailable")
