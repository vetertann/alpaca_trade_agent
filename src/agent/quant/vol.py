"""Realized volatility.

The intraday series is empty at the start of a session, and `iv_rv_ratio` is the
central signal, so there is a daily-bar fallback rather than a None.
"""
from __future__ import annotations

import math
import statistics as stats

TRADING_DAYS = 252
MINUTES_PER_YEAR = TRADING_DAYS * 390


def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def realized_from_closes(closes: list[float], periods_per_year: int) -> float | None:
    rets = _log_returns(closes)
    if len(rets) < 5:
        return None
    return stats.pstdev(rets) * math.sqrt(periods_per_year)


def realized_from_bars(bars: list[dict], window: int = 20) -> float | None:
    """Close-to-close, annualised. `bars` are daily bars, oldest first."""
    closes = [float(b["c"]) for b in bars[-(window + 1):] if b.get("c")]
    return realized_from_closes(closes, TRADING_DAYS)


def ewma_from_bars(bars: list[dict], lam: float = 0.94, window: int = 60) -> float | None:
    """Exponentially weighted, which reacts faster than a flat window."""
    closes = [float(b["c"]) for b in bars[-(window + 1):] if b.get("c")]
    rets = _log_returns(closes)
    if len(rets) < 5:
        return None
    var = rets[0] ** 2
    for r in rets[1:]:
        var = lam * var + (1 - lam) * r * r
    return math.sqrt(var * TRADING_DAYS)


def blend(intraday: float | None, daily: float | None) -> tuple[float | None, str]:
    """Prefer the live series once it has enough points; say which was used."""
    if intraday is not None:
        return intraday, "intraday"
    if daily is not None:
        return daily, "daily_bars"
    return None, "unavailable"
