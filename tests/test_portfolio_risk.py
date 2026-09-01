import datetime as dt

import pytest

from agent.host.portfolio_risk import (assess_admission,
                                       evidence_risk_ceiling,
                                       feasible_quantity_interval,
                                       stress_portfolio)


NOW = dt.datetime(2026, 8, 31, 15, 0, tzinfo=dt.timezone.utc)


def _leg(symbol, strike, kind, side):
    return {"symbol": symbol, "strike": strike, "option_type": kind,
            "side": side, "position_intent": f"{side}_to_open",
            "ratio_qty": 1, "expiry": "2026-09-03"}


def test_exact_solver_caps_a_risk_increasing_candidate():
    scenarios = [
        {"current_book_pnl": -200, "candidate_unit_pnl": -150,
         "spot_expected_move_multiple": 1, "iv_relative_shock": 0},
        {"current_book_pnl": -100, "candidate_unit_pnl": 25,
         "spot_expected_move_multiple": -1, "iv_relative_shock": 0},
    ]
    solved = feasible_quantity_interval(scenarios, loss_limit=500, max_qty=9)
    assert solved["feasible"] is True
    assert solved["minimum_qty"] == 0
    assert solved["maximum_qty"] == 2
    assert solved["allowed_qty"] == 2


def test_exact_solver_allows_repairing_candidate_when_zero_is_breached():
    scenarios = [
        {"current_book_pnl": -700, "candidate_unit_pnl": 120,
         "spot_expected_move_multiple": 1, "iv_relative_shock": 0},
        {"current_book_pnl": -200, "candidate_unit_pnl": -30,
         "spot_expected_move_multiple": -1, "iv_relative_shock": 0},
    ]
    solved = feasible_quantity_interval(scenarios, loss_limit=500, max_qty=10)
    assert solved["feasible"] is True
    assert solved["minimum_qty"] == 2
    assert solved["maximum_qty"] == 10


def test_exact_solver_rejects_unchanged_breached_scenario():
    scenarios = [{"current_book_pnl": -501, "candidate_unit_pnl": 0,
                  "spot_expected_move_multiple": 0, "iv_relative_shock": 0.2}]
    solved = feasible_quantity_interval(scenarios, loss_limit=500, max_qty=10)
    assert solved["feasible"] is False
    assert solved["allowed_qty"] == 0


def test_stress_uses_correlated_spy_qqq_moves_and_candidate_unit():
    structures = [{
        "structure_id": "spy-put", "underlying": "SPY", "qty": 2,
        "legs": [_leg("SPY260903P00100000", 100, "put", "buy")],
    }]
    candidate = {
        "underlying": "QQQ",
        "legs": [_leg("QQQ260903C00200000", 200, "call", "buy")],
    }
    quotes = {
        "SPY260903P00100000": {"bp": 2.0, "ap": 2.2},
        "QQQ260903C00200000": {"bp": 4.0, "ap": 4.2},
    }
    out = stress_portfolio(
        structures, quotes, {"SPY": 100, "QQQ": 200}, NOW,
        candidate=candidate, sigma_by_underlying={"SPY": 0.20, "QQQ": 0.20})
    assert out["status"] == "ok"
    assert len(out["scenarios"]) == 10
    down = next(row for row in out["scenarios"]
                if row["spot_expected_move_multiple"] == -1
                and row["iv_relative_shock"] == 0)
    assert down["spots"]["SPY"] < 100
    assert down["spots"]["QQQ"] < 200
    assert down["candidate_unit_pnl"] < 0
    assert down["current_book_pnl"] > 0
    assert out["provenance"]["baseline"].startswith("executable")


def test_incomplete_leg_data_fails_closed():
    out = stress_portfolio(
        [], {}, {"SPY": 100}, NOW,
        candidate={"underlying": "SPY", "legs": [_leg(
            "SPY260903C00100000", 100, "call", "buy")]})
    assert out["status"] == "incomplete"
    admission = assess_admission(out, equity=100_000, loss_limit_pct=1, ordinary_max_qty=4)
    assert admission["allowed_qty"] == 0


def test_assessment_reports_current_breach_and_resulting_book():
    stress = {"status": "ok", "scenarios": [
        {"current_book_pnl": -600, "candidate_unit_pnl": 60},
        {"current_book_pnl": -100, "candidate_unit_pnl": -20},
    ]}
    out = assess_admission(stress, equity=100_000, loss_limit_pct=0.5,
                           ordinary_max_qty=10)
    assert out["current_breached"] is True
    assert out["allowed_qty"] == 10
    assert out["resulting_breached"] is False
    assert out["resulting_worst_pnl"] == pytest.approx(-300)


def test_evidence_ceiling_distinguishes_all_contest_tiers():
    robust = evidence_risk_ceiling({
        "evaluation": {"candidate": "c1", "edge_by_measure": {
            "lognormal": .1, "bootstrap": .2, "student_t": .05}},
        "ranking": {"stable_top": ["c1"]},
    }, 100_000, robust_pct=4, supported_pct=1.5, partial_pct=.5)
    supported = evidence_risk_ceiling({
        "evaluation": {"candidate": "c1", "edge_by_measure": {
            "lognormal": .1, "bootstrap": .02, "student_t": .05}},
        "ranking": {"stable_top": []},
    }, 100_000, robust_pct=4, supported_pct=1.5, partial_pct=.5)
    partial = evidence_risk_ceiling({
        "evaluation": {"candidate": "c1", "edge_by_measure": {
            "lognormal": .1, "bootstrap": -.01, "student_t": .05}},
        "ranking": {"stable_top": []},
    }, 100_000, robust_pct=4, supported_pct=1.5, partial_pct=.5)
    weak = evidence_risk_ceiling({
        "evaluation": {"candidate": "c1", "edge_by_measure": {
            "lognormal": .1, "bootstrap": -.2, "student_t": -.1}},
        "ranking": {"stable_top": ["c1"]},
    }, 100_000, robust_pct=4, supported_pct=1.5, partial_pct=.5)
    assert (robust["tier"], robust["ceiling_dollars"]) == ("robust", 4000)
    assert (supported["tier"], supported["ceiling_dollars"]) == ("supported", 1500)
    assert (partial["tier"], partial["ceiling_dollars"]) == ("partial", 500)
    assert (weak["tier"], weak["ceiling_dollars"]) == ("insufficient", 0)
