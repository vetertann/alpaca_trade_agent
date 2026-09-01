import pytest
from agent.quant import candidates as cd
from agent.quant import structures as st

EXP = "2026-09-03"


def row(strike, kind, bid, ask, delta=None):
    return {"symbol": f"SPY260903{kind[0].upper()}{strike*1000:08.0f}", "strike": float(strike),
            "option_type": kind, "expiry": EXP, "bid": bid, "ask": ask,
            "mid": (bid + ask) / 2, "spread_pct": (ask - bid) / ((bid + ask) / 2) * 100,
            "delta": delta}


def chain(spot=770.0, n=12, step=1.0):
    """A synthetic chain that decays sensibly away from spot."""
    out = []
    for i in range(-n, n + 1):
        k = spot + i * step
        c_mid = max(spot - k, 0) + 3.0 * (0.5 ** abs(i / 4))
        p_mid = max(k - spot, 0) + 3.0 * (0.5 ** abs(i / 4))
        out.append(row(k, "call", round(c_mid - 0.05, 2), round(c_mid + 0.05, 2)))
        out.append(row(k, "put", round(p_mid - 0.05, 2), round(p_mid + 0.05, 2)))
    return out


def test_enumerates_across_families():
    cands = cd.enumerate_structures(chain(), 770.0)
    fams = {c.family for c in cands}
    assert {"vertical_call", "vertical_put", "straddle", "iron_condor"} <= fams
    assert len(cands) > 30


def test_every_candidate_has_positive_max_profit():
    """Dominated structures are dropped before the model ever sees them."""
    for c in cd.enumerate_structures(chain(), 770.0):
        assert c.max_loss > 0
        assert c.max_profit == st.UNBOUNDED or c.max_profit > 0


def test_prices_use_ask_to_buy_and_bid_to_sell():
    cands = cd.enumerate_structures(chain(), 770.0, families=("vertical_call",),
                                    widths=(5,))
    debit = next(c for c in cands if c.net_price > 0)
    legs = {l.side: l for l in debit.legs}
    rows = {r["strike"]: r for r in chain() if r["option_type"] == "call"}
    expected = rows[legs["buy"].strike]["ask"] - rows[legs["sell"].strike]["bid"]
    assert debit.net_price == pytest.approx(expected)


def test_spread_cost_is_reported_against_max_loss():
    cands = cd.enumerate_structures(chain(), 770.0, families=("vertical_call",))
    assert all(c.spread_cost_pct >= 0 for c in cands)
    assert any(c.spread_cost_pct > 0 for c in cands)


def test_filter_drops_poor_risk_reward():
    cands = cd.enumerate_structures(chain(), 770.0)
    kept = cd.filter_candidates(cands, min_risk_reward=0.5)
    assert all(c.max_profit == st.UNBOUNDED or c.risk_reward >= 0.5 for c in kept)
    assert len(kept) < len(cands)


def test_filter_respects_a_max_loss_cap():
    cands = cd.enumerate_structures(chain(), 770.0)
    kept = cd.filter_candidates(cands, max_loss_cap=300.0, min_risk_reward=0.0,
                                max_spread_cost_pct=1e9)
    assert kept and all(c.max_loss <= 300.0 for c in kept)


def test_iron_condor_has_four_legs_and_bounded_risk():
    cands = cd.enumerate_structures(chain(), 770.0, families=("iron_condor",))
    assert cands
    ic = cands[0]
    assert len(ic.legs) == 4 and ic.max_profit != st.UNBOUNDED and ic.max_loss > 0


def test_straddle_is_long_premium():
    cands = cd.enumerate_structures(chain(), 770.0, families=("straddle",))
    assert cands and all(c.net_price > 0 for c in cands)


def test_json_shape_is_model_ready():
    c = cd.enumerate_structures(chain(), 770.0, families=("vertical_call",))[0]
    j = c.to_json()
    assert {"id", "family", "legs", "net_price", "max_loss", "risk_reward",
            "spot_at_enumeration", "breakevens", "pnl_if_expired_now",
            "net_delta", "dollar_delta_per_1pct"} <= set(j)
    assert all({"symbol", "side", "position_intent", "strike"} <= set(l) for l in j["legs"])


def test_candidate_aggregates_signed_leg_delta_host_side():
    rows = chain()
    for item in rows:
        item["delta"] = (0.70 - (item["strike"] - 765.0) * 0.02
                         if item["option_type"] == "call" else -0.45)
    candidate = cd.enumerate_structures(
        rows, 770.0, families=("vertical_call",), widths=(5,))[0]
    by_symbol = {item["symbol"]: item for item in rows}
    expected = sum(leg.sign * leg.ratio_qty * by_symbol[leg.symbol]["delta"]
                   for leg in candidate.legs)
    assert candidate.detail["net_delta"] == pytest.approx(expected)
    assert candidate.detail["dollar_delta_per_1pct"] == pytest.approx(
        round(expected * 770.0, 2))


def test_candidate_ids_are_unique_across_underlyings():
    spy = cd.enumerate_structures(chain(), 770.0, underlying="SPY",
                                  families=("vertical_call",))[0]
    qqq = cd.enumerate_structures(chain(), 770.0, underlying="QQQ",
                                  families=("vertical_call",))[0]
    assert spy.id.startswith("SPY:") and qqq.id.startswith("QQQ:")
    assert spy.id != qqq.id
