"""Host-side logic that does not need a broker."""
import pytest
from agent.host.capabilities import _diverse
from agent.quant import candidates as cd


class Fake:
    def __init__(self, family, cost, cid):
        self.family, self.spread_cost_pct, self.id = family, cost, cid


def test_diverse_samples_across_families():
    """Ranking by risk/reward puts every unbounded-profit structure first, because
    `inf` always wins, and the model then never sees a vertical or a condor."""
    cands = ([Fake("straddle", i, f"s{i}") for i in range(20)]
             + [Fake("vertical_call", 10 + i, f"v{i}") for i in range(20)]
             + [Fake("iron_condor", 20 + i, f"c{i}") for i in range(20)])
    got = _diverse(cands, 9)
    assert len(got) == 9
    assert {c.family for c in got} == {"straddle", "vertical_call", "iron_condor"}
    assert sum(1 for c in got if c.family == "straddle") == 3


def test_diverse_prefers_cheapest_to_cross_within_a_family():
    cands = [Fake("straddle", 5.0, "expensive"), Fake("straddle", 1.0, "cheap")]
    assert _diverse(cands, 1)[0].id == "cheap"


def test_diverse_handles_fewer_candidates_than_the_limit():
    assert len(_diverse([Fake("straddle", 1.0, "a")], 10)) == 1


def test_diverse_on_empty_input():
    assert _diverse([], 5) == []
