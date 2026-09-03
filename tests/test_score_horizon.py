import datetime as dt

import pytest

from agent.config import AUTONOMOUS_TRADING_END, ET, WINDOW_CLOSE
from agent.host.capabilities import Capabilities, CapabilityError
from agent.quant import candidates as cd
from agent.quant import measures as ms
from agent.quant import score_horizon
from agent.types import Leg


def later_candidate():
    leg = Leg("SPY260911C00765000", 1, "buy", "buy_to_open",
              765.0, "call", dt.date(2026, 9, 11))
    return cd.Candidate(
        "later-call", "vertical_call", "SPY", "2026-09-11", [leg],
        5.0, 500.0, float("inf"), 0.0, 1.0,
        {"leg_valuation_inputs": {
            leg.symbol: {"iv": 0.20, "mid": 5.0, "half_spread": 0.05},
        }})


def test_later_contract_is_evaluated_at_official_equity_mark():
    now = dt.datetime(2026, 9, 1, 12, 0, tzinfo=ET)
    out = score_horizon.candidate_horizon("2026-09-11", now)

    assert out["evaluation_at"] == WINDOW_CLOSE.isoformat(timespec="seconds")
    assert out["residual_calendar_days_at_evaluation"] == 8.0
    assert out["score_horizon_trading_days"] == pytest.approx(2.615385, abs=1e-6)
    assert "residual time value" in out["valuation_basis"]
    assert out["horizon_kind"] == "official_score"


def test_friday_post_submission_session_uses_friday_close_horizon():
    now = dt.datetime(2026, 9, 4, 10, 0, tzinfo=ET)
    out = score_horizon.candidate_horizon("2026-09-11", now)

    assert out["evaluation_at"] == AUTONOMOUS_TRADING_END.isoformat(timespec="seconds")
    assert out["horizon_kind"] == "post_submission_paper_session"
    assert out["score_horizon_trading_days"] == pytest.approx(6 / 6.5, abs=1e-6)
    assert "Friday post-submission" in out["valuation_basis"]


def test_score_mark_retains_time_value_and_executable_spread():
    candidate = later_candidate()

    value = score_horizon.executable_value(candidate, 765.0, WINDOW_CLOSE)

    # At-the-money with eight days left is worth more than intrinsic zero, even
    # after the observed $0.05 half-spread is retained.
    assert value > 0


def test_post_window_evaluation_requires_host_owned_measure_horizon():
    caps = object.__new__(Capabilities)
    candidate = later_candidate()
    caps._candidates = {candidate.id: candidate}
    caps._measures = {"manual": [ms.Measure("certain", (765.0,))]}
    caps._measure_context = {"manual": {
        "symbol": "SPY", "sigma": 0.2, "days": 2.0,
        "horizon_source": "caller_supplied",
    }}

    with pytest.raises(CapabilityError, match="must use vol.measures_for"):
        caps._vol_evaluate(candidate.id, "manual")


def test_post_window_evaluation_reports_mark_horizon_and_iv_sensitivity():
    caps = object.__new__(Capabilities)
    candidate = later_candidate()
    caps._candidates = {candidate.id: candidate}
    caps._measures = {"score": [ms.Measure("certain", (765.0,))]}
    caps._measure_context = {"score": {
        "symbol": "SPY", "sigma": 0.2, "days": 2.0,
        "horizon_source": "candidate_score_horizon",
        "evaluation_at": WINDOW_CLOSE.isoformat(timespec="seconds"),
    }}
    caps._evaluated = []

    out = caps._vol_evaluate(candidate.id, "score")

    assert out["evaluation_at"] == WINDOW_CLOSE.isoformat(timespec="seconds")
    assert out["valuation_basis"].startswith("Thursday score-time")
    assert set(out["score_horizon_iv_sensitivity"]) == {
        "iv_80pct", "iv_100pct", "iv_120pct"}
    assert out["score_horizon_iv_sensitivity"]["iv_120pct"] \
        > out["score_horizon_iv_sensitivity"]["iv_80pct"]
