"""Token bucket. Generated code cannot exceed the budget however it loops."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_min: int, name: str = "bucket"):
        self.capacity = float(rate_per_min)
        self.tokens = float(rate_per_min)
        self.refill_per_s = rate_per_min / 60.0
        self.name = name
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.waits = 0

    def take(self, n: int = 1, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self._last) * self.refill_per_s)
                self._last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                shortfall = n - self.tokens
                sleep_for = shortfall / self.refill_per_s
            if time.monotonic() + sleep_for > deadline:
                raise TimeoutError(f"{self.name}: rate limit wait exceeded {timeout}s")
            self.waits += 1
            time.sleep(min(sleep_for, 0.25))


# Two independent budgets: market data and trading do not compete.
DATA = TokenBucket(200, "data")
TRADING = TokenBucket(200, "trading")
