import time
import pytest
from agent.host.limiter import TokenBucket


def test_allows_a_burst_up_to_capacity():
    b = TokenBucket(60, "t")
    for _ in range(60):
        b.take()
    assert b.waits == 0


def test_blocks_past_capacity_then_refills():
    b = TokenBucket(600, "t")          # 10/second
    for _ in range(600):
        b.take()
    t0 = time.monotonic()
    b.take()
    assert time.monotonic() - t0 > 0.01
    assert b.waits > 0


def test_timeout_raises_rather_than_hanging():
    b = TokenBucket(6, "t")            # 0.1/second
    for _ in range(6):
        b.take()
    with pytest.raises(TimeoutError, match="rate limit"):
        b.take(timeout=0.3)


def test_budgets_are_independent():
    from agent.host import limiter
    assert limiter.DATA is not limiter.TRADING
    before = limiter.TRADING.tokens
    limiter.DATA.take(5)
    assert limiter.TRADING.tokens == before
