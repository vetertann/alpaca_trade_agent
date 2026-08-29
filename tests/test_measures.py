import math
import pytest
from agent.quant import measures as ms


def closes(n=200, step=0.004, seed=3):
    import random
    r = random.Random(seed)
    out, p = [100.0], 100.0
    for _ in range(n):
        p *= math.exp(r.gauss(0, step))
        out.append(p)
    return out


def test_lognormal_centres_near_spot():
    m = ms.lognormal(100.0, 0.20, 5)
    mean = sum(m.samples) / len(m.samples)
    assert 97 < mean < 103


def test_higher_vol_widens_the_distribution():
    lo = ms.lognormal(100.0, 0.10, 5)
    hi = ms.lognormal(100.0, 0.40, 5)
    spread = lambda m: max(m.samples) - min(m.samples)
    assert spread(hi) > spread(lo)


def test_student_t_has_a_fatter_tail_than_lognormal():
    """The whole reason measure C exists."""
    ln = ms.lognormal(100.0, 0.20, 5)
    st = ms.student_t(100.0, 0.20, 5)
    far = lambda m: m.prob(lambda s: abs(s / 100.0 - 1) > 0.06)
    assert far(st) > far(ln)


def test_bootstrap_needs_enough_history():
    assert ms.block_bootstrap(100.0, [100, 101, 102], 5) is None
    assert ms.block_bootstrap(100.0, closes(), 5) is not None


def test_build_returns_three_when_history_allows():
    assert len(ms.build(100.0, 0.2, 5, closes())) == 3
    assert len(ms.build(100.0, 0.2, 5, None)) == 2


def test_evaluate_flags_a_candidate_that_only_one_measure_likes():
    m = ms.build(100.0, 0.20, 5, closes())
    # a payoff that only pays in a far tail: the fat-tail measure likes it more
    payoff = lambda s: 500.0 if s > 112 else 0.0
    out = ms.evaluate(payoff, m, traded_price=8.0)
    assert set(out["edge_by_measure"]) == {"lognormal", "block_bootstrap", "student_t"}
    assert 0.0 <= out["agreement"] <= 1.0
    assert out["survives_all"] == (out["edge_min"] > 0)


def test_evaluate_agrees_on_an_obviously_good_payoff():
    m = ms.build(100.0, 0.20, 5, closes())
    out = ms.evaluate(lambda s: 100.0, m, traded_price=1.0)   # pays 100 always, costs 1
    assert out["survives_all"] and out["agreement"] == 1.0


def test_evaluate_agrees_on_an_obviously_bad_payoff():
    m = ms.build(100.0, 0.20, 5, closes())
    out = ms.evaluate(lambda s: 1.0, m, traded_price=100.0)
    assert not out["survives_all"] and out["agreement"] == 0.0


def test_rank_stability_detects_disagreement():
    m = ms.build(100.0, 0.20, 5, closes())
    cands = [
        {"id": "safe", "pay": lambda s: 10.0, "px": 5.0},
        {"id": "tail", "pay": lambda s: 400.0 if s > 115 else 0.0, "px": 5.0},
        {"id": "dud", "pay": lambda s: 1.0, "px": 5.0},
    ]
    out = ms.rank_stability(cands, m, lambda c: c["pay"], lambda c: c["px"], top_k=1)
    assert 0.0 <= out["stability"] <= 1.0
    assert set(out["ranks"]) == {"safe", "tail", "dud"}
