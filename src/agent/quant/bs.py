"""Black-Scholes pricing, Greeks, and an implied-volatility solver.

Local calculation is canonical. Alpaca serves Greeks for contracts with a valid
two-sided quote, and those are used as a cross-check rather than as the source.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2PI = math.sqrt(2.0 * math.pi)
DAYS_PER_YEAR = 365.0
MIN_T = 1.0 / (DAYS_PER_YEAR * 24 * 60)   # one minute, so expiry-day maths stays finite


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def _d1_d2(s: float, k: float, t: float, sigma: float, r: float, q: float) -> tuple[float, float]:
    v = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / v
    return d1, d1 - v


def price(s: float, k: float, t: float, sigma: float, kind: str,
          r: float = 0.04, q: float = 0.0) -> float:
    """Black-Scholes-Merton price. `kind` is 'call' or 'put'."""
    t = max(t, MIN_T)
    if sigma <= 0:
        intrinsic = (s - k) if kind == "call" else (k - s)
        return max(intrinsic, 0.0) * math.exp(-r * t)
    d1, d2 = _d1_d2(s, k, t, sigma, r, q)
    if kind == "call":
        return s * math.exp(-q * t) * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * math.exp(-q * t) * _ncdf(-d1)


def greeks(s: float, k: float, t: float, sigma: float, kind: str,
           r: float = 0.04, q: float = 0.0) -> Greeks:
    """Greeks in the conventional retail units: theta and rho per day / per 1%."""
    t = max(t, MIN_T)
    d1, d2 = _d1_d2(s, k, t, sigma, r, q)
    sqrt_t = math.sqrt(t)
    disc_q, disc_r = math.exp(-q * t), math.exp(-r * t)

    gamma = disc_q * _npdf(d1) / (s * sigma * sqrt_t)
    vega = s * disc_q * _npdf(d1) * sqrt_t / 100.0
    common_theta = -(s * disc_q * _npdf(d1) * sigma) / (2.0 * sqrt_t)

    if kind == "call":
        delta = disc_q * _ncdf(d1)
        theta = (common_theta - r * k * disc_r * _ncdf(d2) + q * s * disc_q * _ncdf(d1))
        rho = k * t * disc_r * _ncdf(d2) / 100.0
    else:
        delta = -disc_q * _ncdf(-d1)
        theta = (common_theta + r * k * disc_r * _ncdf(-d2) - q * s * disc_q * _ncdf(-d1))
        rho = -k * t * disc_r * _ncdf(-d2) / 100.0

    return Greeks(delta, gamma, theta / DAYS_PER_YEAR, vega, rho)


def implied_vol(target: float, s: float, k: float, t: float, kind: str,
                r: float = 0.04, q: float = 0.0,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-7,
                iters: int = 100) -> float | None:
    """Solve for sigma by bisection. Returns None when the quote is unusable.

    A price below intrinsic value cannot be produced by any volatility, so it is
    bad data rather than an opportunity.
    """
    t = max(t, MIN_T)
    intrinsic = max((s - k) if kind == "call" else (k - s), 0.0) * math.exp(-r * t)
    if target < intrinsic - 1e-6 or target <= 0:
        return None
    lo_p, hi_p = price(s, k, t, lo, kind, r, q), price(s, k, t, hi, kind, r, q)
    if not (lo_p <= target <= hi_p):
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if price(s, k, t, mid, kind, r, q) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def year_fraction(now, expiry) -> float:
    """Calendar-time year fraction, floored so expiry day never divides by zero."""
    return max((expiry - now).total_seconds() / (DAYS_PER_YEAR * 86400.0), MIN_T)
