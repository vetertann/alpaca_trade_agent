import math
from agent.quant import bs


def test_put_call_parity():
    s, k, t, sig, r = 100.0, 95.0, 0.25, 0.22, 0.04
    c = bs.price(s, k, t, sig, "call", r)
    p = bs.price(s, k, t, sig, "put", r)
    assert abs((c - p) - (s - k * math.exp(-r * t))) < 1e-9


def test_implied_vol_roundtrip():
    s, k, t, sig = 100.0, 105.0, 0.1, 0.31
    target = bs.price(s, k, t, sig, "call")
    assert abs(bs.implied_vol(target, s, k, t, "call") - sig) < 1e-4


def test_price_below_intrinsic_is_unusable():
    # deep ITM call quoted under intrinsic -- no sigma produces this
    assert bs.implied_vol(1.0, 200.0, 100.0, 0.05, "call") is None


def test_zero_price_is_unusable():
    assert bs.implied_vol(0.0, 100.0, 100.0, 0.05, "call") is None


def test_atm_delta_near_half():
    g = bs.greeks(100.0, 100.0, 0.02, 0.20, "call")
    assert 0.45 < g.delta < 0.60
    assert g.gamma > 0 and g.vega > 0 and g.theta < 0
