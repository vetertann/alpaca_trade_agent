import datetime as dt
from types import SimpleNamespace

import pytest

from agent.config import ET
from agent.host import portfolio
from agent.host.risk_params import DEFAULT


def test_short_spread_snapshot_exposes_actionable_live_risk_and_trajectory():
    structure = {
        "structure_id": "sid-qqq", "thesis_id": "th-qqq", "underlying": "QQQ",
        "family": "vertical_call", "qty": 1, "cost_basis": -633,
        "unrealized_pl": -100, "premium_at_risk": 367,
        "legs": [
            {"symbol": "QQQ260901C00707000", "ratio_qty": 1, "side": "sell",
             "position_intent": "sell_to_open", "strike": 707,
             "option_type": "call", "expiry": "2026-09-01"},
            {"symbol": "QQQ260901C00717000", "ratio_qty": 1, "side": "buy",
             "position_intent": "buy_to_open", "strike": 717,
             "option_type": "call", "expiry": "2026-09-01"},
        ],
    }
    quotes = {
        "QQQ260901C00707000": {"bp": 7.9, "ap": 8.0},
        "QQQ260901C00717000": {"bp": 1.0, "ap": 1.1},
    }
    now = dt.datetime(2026, 8, 31, 14, 0, tzinfo=ET)
    thesis = SimpleNamespace(exit_at="2026-09-01T15:45:00-04:00")

    snap = portfolio.snapshot(
        {"equity": 99_900}, {"structures": [structure], "premium_at_risk": 367,
                              "realised_loss": 0}, {"th-qqq": thesis}, quotes,
        {"QQQ": 713}, now, DEFAULT)
    row = snap["structures"][0]

    assert row["structure_id"] == "sid-qqq"
    assert row["current_exit_value_per_unit"] == -7.0
    assert row["current_close_price_per_unit"] == 7.0
    assert row["executable_unrealized_pl"] == pytest.approx(-67)
    quality = row["exit_quote_quality"]
    assert quality["all_exit_leg_quotes_valid"] is True
    assert quality["missing_exit_leg_symbols"] == []
    assert quality["close_crossing_cost_from_midpoint_dollars"] == 10.0
    assert quality["aggregate_leg_bid_ask_width_dollars"] == 20.0
    assert quality["widest_leg_bid_ask_spread_pct_of_mid"] == pytest.approx(
        100 * 0.1 / 1.05, abs=1e-4)
    assert row["loss_stop"] == pytest.approx(183.5)
    assert row["stop_progress"] == pytest.approx(100 / 183.5, abs=1e-4)
    assert row["pnl_if_expired_now_per_unit"] == pytest.approx(33)
    assert row["minutes_to_exit"] > 0

    later = {**snap, "observed_at": "later",
             "structures": [{**row, "broker_unrealized_pl": -120}]}
    view = portfolio.with_trajectories(later, [snap, later])
    assert [point["unrealized_pl"] for point in
            view["structures"][0]["pnl_trajectory"]] == [-100, -120]


def test_recent_executable_pnl_variation_is_clearly_bounded_and_labelled():
    base = dt.datetime(2026, 8, 31, 14, 0, tzinfo=dt.timezone.utc)
    history = []
    for index, pnl in enumerate((0, 10, 5, 25)):
        history.append({
            "observed_at": (base + dt.timedelta(seconds=10 * index)).isoformat(),
            "equity": 100_000,
            "total_unrealized_pl": pnl,
            "structures": [{"structure_id": "sid", "broker_unrealized_pl": pnl,
                            "executable_unrealized_pl": pnl,
                            "stop_progress": 0, "spot": 100}],
        })

    view = portfolio.with_trajectories(history[-1], history)
    variation = view["structures"][0]["recent_executable_pnl_variation"]

    assert variation == {
        "lookback_seconds": 30,
        "valid_executable_pnl_observation_count": 4,
        "successive_change_count": 3,
        "median_absolute_successive_change_dollars": 10.0,
        "p90_absolute_successive_change_dollars": 20.0,
        "maximum_absolute_successive_change_dollars": 20.0,
    }


def test_variation_does_not_bridge_a_missing_quote_interval():
    base = dt.datetime(2026, 8, 31, 14, 0, tzinfo=dt.timezone.utc)
    values = (10, None, 30, 35)
    history = [{
        "observed_at": (base + dt.timedelta(seconds=10 * index)).isoformat(),
        "structures": [{"structure_id": "sid",
                        "executable_unrealized_pl": value}],
    } for index, value in enumerate(values)]

    variation = portfolio.with_trajectories(
        history[-1], history)["structures"][0]["recent_executable_pnl_variation"]
    assert variation["valid_executable_pnl_observation_count"] == 3
    assert variation["successive_change_count"] == 1
    assert variation["maximum_absolute_successive_change_dollars"] == 5.0


def test_missing_underlying_does_not_invent_expiry_pnl():
    structure = {
        "structure_id": "orphan:x", "underlying": "SPY", "qty": 1,
        "cost_basis": 100, "unrealized_pl": 0, "premium_at_risk": 100,
        "legs": [{"symbol": "SPY260901C00100000", "ratio_qty": 1, "side": "buy",
                  "position_intent": "buy_to_open", "strike": 100,
                  "option_type": "call", "expiry": "2026-09-01"}],
    }
    row = portfolio.structure_view(
        structure, None, {}, {}, dt.datetime(2026, 8, 31, 14, 0, tzinfo=ET), DEFAULT)
    assert row["spot"] is None
    assert row["pnl_if_expired_now_per_unit"] is None


def test_finite_debit_profit_target_is_half_maximum_profit_not_half_debit():
    structure = {
        "structure_id": "sid-debit", "thesis_id": "th-debit", "underlying": "QQQ",
        "family": "vertical_put", "qty": 8, "cost_basis": 2568,
        "unrealized_pl": 0, "premium_at_risk": 2568,
        "legs": [
            {"symbol": "QQQ260902P00709000", "ratio_qty": 1, "side": "buy",
             "position_intent": "buy_to_open", "strike": 709,
             "option_type": "put", "expiry": "2026-09-02"},
            {"symbol": "QQQ260902P00699000", "ratio_qty": 1, "side": "sell",
             "position_intent": "sell_to_open", "strike": 699,
             "option_type": "put", "expiry": "2026-09-02"},
        ],
    }
    thesis = SimpleNamespace(
        exit_at="2026-09-02T15:00:00-04:00",
        enforced_exit_policy={
            "schema_version": 2, "candidate_id": "c",
            "profit_target": {"kind": "maximum_profit_fraction", "value": .5}})
    quotes = {
        "QQQ260902P00709000": {"bp": 4, "ap": 4.1},
        "QQQ260902P00699000": {"bp": 1, "ap": 1.1},
    }
    row = portfolio.snapshot(
        {"equity": 100_000}, {"structures": [structure], "premium_at_risk": 2568,
                               "realised_loss": 0}, {"th-debit": thesis}, quotes,
        {"QQQ": 705}, dt.datetime(2026, 9, 1, 14, 0, tzinfo=ET), DEFAULT
    )["structures"][0]
    assert row["profit_target"] == pytest.approx(2716)
    assert row["profit_target"] != pytest.approx(1284)


def test_explicit_credit_fraction_on_debit_fails_closed_without_basis_fallback():
    structure = {
        "structure_id": "sid", "underlying": "SPY", "family": "vertical_call",
        "qty": 1, "cost_basis": 270, "unrealized_pl": 200,
        "premium_at_risk": 270,
        "legs": [
            {"symbol": "SPY260903C00770000", "ratio_qty": 1, "side": "buy",
             "position_intent": "buy_to_open", "strike": 770,
             "option_type": "call", "expiry": "2026-09-03"},
            {"symbol": "SPY260903C00775000", "ratio_qty": 1, "side": "sell",
             "position_intent": "sell_to_open", "strike": 775,
             "option_type": "call", "expiry": "2026-09-03"},
        ],
    }
    thesis = SimpleNamespace(exit_at="", enforced_exit_policy={
        "schema_version": 2, "candidate_id": "bad", "premium_type": "long",
        "profit_target": {"kind": "entry_credit_fraction", "value": .5}})
    row = portfolio.structure_view(
        structure, thesis, {}, {"SPY": 772},
        dt.datetime(2026, 9, 1, 14, 0, tzinfo=ET), DEFAULT)

    assert row["profit_target"] == 0
    assert row["profit_target_policy"]["validation_status"] == "invalid"
    assert any("net-credit" in error
               for error in row["profit_target_policy"]["validation_errors"])


def test_maximum_profit_fraction_on_unbounded_straddle_fails_closed():
    structure = {
        "structure_id": "straddle", "underlying": "SPY", "family": "straddle",
        "qty": 1, "cost_basis": 1000, "unrealized_pl": 800,
        "premium_at_risk": 1000,
        "legs": [
            {"symbol": "SPY260903C00770000", "ratio_qty": 1, "side": "buy",
             "position_intent": "buy_to_open", "strike": 770,
             "option_type": "call", "expiry": "2026-09-03"},
            {"symbol": "SPY260903P00770000", "ratio_qty": 1, "side": "buy",
             "position_intent": "buy_to_open", "strike": 770,
             "option_type": "put", "expiry": "2026-09-03"},
        ],
    }
    thesis = SimpleNamespace(exit_at="", enforced_exit_policy={
        "schema_version": 2, "candidate_id": "bad", "premium_type": "long",
        "profit_target": {"kind": "maximum_profit_fraction", "value": .5}})
    row = portfolio.structure_view(
        structure, thesis, {}, {"SPY": 770},
        dt.datetime(2026, 9, 1, 14, 0, tzinfo=ET), DEFAULT)

    assert row["profit_target"] == 0
    assert row["profit_target_policy"]["validation_status"] == "invalid"
    assert any("finite-profit" in error
               for error in row["profit_target_policy"]["validation_errors"])
