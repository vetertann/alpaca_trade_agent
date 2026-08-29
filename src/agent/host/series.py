"""Rolling in-memory series.

Historical endpoints exclude the most recent fifteen minutes, so the question a
cycle most needs to answer -- what moved since the last cycle -- cannot be served
from history for the interval that matters. The watcher accumulates its own
series from the stream instead.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics as stats
from collections import deque
from pathlib import Path

MINUTES_PER_YEAR = 252 * 390


class RollingSeries:
    """Per-symbol price history, second and minute resolution, session-scoped."""

    def __init__(self, max_seconds: int = 3600, max_minutes: int = 800):
        self.sec: dict[str, deque] = {}
        self.min: dict[str, deque] = {}
        self._max_s, self._max_m = max_seconds, max_minutes
        self._cur_min: dict[str, tuple[int, float]] = {}

    def observe(self, symbol: str, price: float, when: dt.datetime) -> None:
        if price <= 0:
            return
        s = self.sec.setdefault(symbol, deque(maxlen=self._max_s))
        s.append((when, price))
        bucket = int(when.timestamp() // 60)
        cur = self._cur_min.get(symbol)
        if cur and cur[0] != bucket:
            m = self.min.setdefault(symbol, deque(maxlen=self._max_m))
            m.append((dt.datetime.fromtimestamp(cur[0] * 60, dt.timezone.utc), cur[1]))
        self._cur_min[symbol] = (bucket, price)

    # ---- reads -------------------------------------------------------------
    def last(self, symbol: str) -> float | None:
        s = self.sec.get(symbol)
        return s[-1][1] if s else None

    def minute_closes(self, symbol: str) -> list[float]:
        return [p for _, p in self.min.get(symbol, ())]

    def session_range(self, symbol: str) -> tuple[float, float] | None:
        prices = [p for _, p in self.sec.get(symbol, ())]
        return (min(prices), max(prices)) if prices else None

    def move_since(self, symbol: str, when: dt.datetime) -> float | None:
        """Fractional move from the first observation at or after `when` to now."""
        s = self.sec.get(symbol)
        if not s:
            return None
        ref = next((p for t, p in s if t >= when), None)
        return None if not ref else s[-1][1] / ref - 1.0

    def realized_vol(self, symbol: str, lookback: int = 60) -> float | None:
        """Annualised from minute log returns. The core volatility-state input."""
        closes = self.minute_closes(symbol)[-(lookback + 1):]
        if len(closes) < 12:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 10:
            return None
        return stats.pstdev(rets) * math.sqrt(MINUTES_PER_YEAR)

    # ---- durability --------------------------------------------------------
    def checkpoint(self, path: str | Path) -> None:
        """A restart mid-session recovers its recent window rather than starting blind."""
        payload = {sym: [(t.isoformat(), p) for t, p in dq] for sym, dq in self.min.items()}
        Path(path).write_text(json.dumps(payload))

    def restore(self, path: str | Path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        for sym, rows in json.loads(p.read_text()).items():
            dq = self.min.setdefault(sym, deque(maxlen=self._max_m))
            for iso, price in rows:
                dq.append((dt.datetime.fromisoformat(iso), price))
        return True
