"""Host-side logic that does not need a broker."""
import pytest
import datetime as dt
from agent.host.capabilities import _diverse
from agent.host.capabilities import Capabilities, CapabilityError
from agent.host.action_triggers import ActionTriggerStore
from agent.host.exit_policy import ExitPolicyStore
from agent.quant import candidates as cd
from agent.types import Leg


class Fake:
    def __init__(self, family, cost, cid, expiry="2026-09-03"):
        self.family, self.spread_cost_pct, self.id = family, cost, cid
        self.expiry = expiry


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


def test_diverse_covers_the_calendar_before_repeating_an_expiry():
    cands = [Fake("iron_condor", 1, f"{expiry}-{i}", expiry)
             for expiry in ("2026-09-03", "2026-09-11", "2027-01-15")
             for i in range(3)]

    got = _diverse(cands, 3)

    assert {candidate.expiry for candidate in got} == {
        "2026-09-03", "2026-09-11", "2027-01-15"}


def test_diverse_samples_the_full_calendar_when_limit_is_smaller():
    expiries = [f"2026-{month:02d}-01" for month in range(1, 7)]
    got = _diverse([Fake("straddle", 1, expiry, expiry) for expiry in expiries], 2)
    assert [candidate.expiry for candidate in got] == [expiries[0], expiries[-1]]


def test_enumerate_empty_result_keeps_the_documented_shape():
    caps = object.__new__(Capabilities)
    caps.params = type("Params", (), {"max_spread_pct_of_mid": 20.0})()
    caps._options_tradeable_chain = lambda *args, **kwargs: []
    caps._market_spot = lambda symbol: 100.0
    out = caps._options_enumerate("SPY", "2026-09-03", "2026-09-03")
    assert out == {"spot": 100.0, "generated": 0, "kept": 0, "families": [],
                   "expiry_coverage": {"generated": {}, "kept": {},
                                       "returned": {}},
                   "note": "no tradeable contracts under the liquidity gate for that range",
                   "candidates": []}


def test_options_expiries_has_no_local_calendar_cutoff():
    caps = object.__new__(Capabilities)
    calls = []
    caps.rest = type("Rest", (), {"contracts": lambda _self, underlying, gte, lte: (
        calls.append((underlying, gte, lte)) or [
            {"expiration_date": "2026-09-03"},
            {"expiration_date": "2028-12-15"},
        ])})()

    out = caps._options_expiries("spy")

    assert out["expiries"] == ["2026-09-03", "2028-12-15"]
    assert calls[0][0] == "SPY" and calls[0][2] is None
    assert "all active broker-listed" in out["eligibility"]


def test_evaluate_many_batches_and_deduplicates_candidate_ids():
    caps = object.__new__(Capabilities)
    seen = []
    caps._vol_evaluate = lambda candidate_id, handle, **_kwargs: (
        seen.append((candidate_id, handle)) or {"candidate": candidate_id})

    out = caps._vol_evaluate_many(["a", "b", "a"], "m1")

    assert list(out) == ["a", "b"]
    assert seen == [("a", "m1"), ("b", "m1")]


def test_rank_reuses_batch_capital_scores_without_repricing_every_candidate():
    caps = object.__new__(Capabilities)
    caps._candidates = {candidate_id: candidate_id for candidate_id in ("a", "b")}
    caps._measures = {"m1": [type("M", (), {"name": name})()
                             for name in ("lognormal", "block_bootstrap", "student_t")]}
    caps._measure_context = {"m1": {"days": 2}}
    caps._candidate_signature = lambda candidate: candidate
    caps._ranked = []
    caps._evaluated = [{
        "candidate": candidate_id, "handle": "m1", "result": {
            "capital_day_score_by_measure": {
                "lognormal": score, "block_bootstrap": score,
                "student_t": score}}
    } for candidate_id, score in (("a", 0.2), ("b", 0.1))]

    out = caps._vol_rank(["a", "b"], "m1", top_k=1)

    assert out["stable_top"] == ["a"]
    assert out["stability"] == 1.0


def test_direction_normalizes_breakeven_and_current_book_exposure():
    expiry = dt.date(2026, 9, 1)
    legs = [Leg("QQQ260901C00707000", 1, "sell", "sell_to_open",
                707.0, "call", expiry),
            Leg("QQQ260901C00717000", 1, "buy", "buy_to_open",
                717.0, "call", expiry)]
    candidate = cd.Candidate("qqq-credit", "vertical_call", "QQQ",
                             expiry.isoformat(), legs, -6.33, 367.0, 633.0,
                             1000.0, 1.0,
                             {"net_delta": -0.41,
                              "dollar_delta_per_1pct": -292.0})
    caps = object.__new__(Capabilities)
    caps._candidates = {candidate.id: candidate}
    caps._market_spot = lambda symbol: 713.645
    caps.open_positions = []
    caps._directional_context_checked = [{
        "symbol": "QQQ",
        "result": {"classification": "bullish", "strength": "moderate"},
    }]
    out = caps._risk_direction(candidate.id, sigma=0.10, days=1)
    assert out["breakevens"] == pytest.approx([713.33])
    assert out["pnl_if_expired_now"] < 0
    assert out["nearest_breakeven"]["points_from_spot"] == pytest.approx(-0.315, abs=1e-4)
    assert out["nearest_breakeven"]["expected_moves_from_spot"] < 0
    assert out["current_book_direction"]["total_dollar_delta_per_1pct"] == 0
    assert out["candidate_bias"] == "bearish"
    assert out["directionality"] == "direction-led"
    assert out["directional_alignment"] == "conflicted"
    assert out["market_context_evidence_recorded"]
    assert out["expiry_pnl_scenarios"]["up_1_expected_move"]["pnl_per_unit"] < 0


def test_direction_rejects_invalid_distribution_scale():
    caps = object.__new__(Capabilities)
    caps._candidates = {"x": type("Candidate", (), {"underlying": "SPY"})()}
    with pytest.raises(CapabilityError, match="sigma"):
        caps._risk_direction("x", sigma=0, days=1)


def test_market_directional_context_records_exact_underlying_for_presubmit():
    class Series:
        def directional_contexts(self, symbols, now=None):
            return {symbol: {"symbol": symbol, "classification": "bullish",
                             "observed_at_et": "2026-09-01T11:00:00-04:00"}
                    for symbol in symbols}

    caps = object.__new__(Capabilities)
    caps.series = Series()
    caps._directional_context_checked = []

    out = caps._market_directional_context("spy")

    assert out["symbol"] == "SPY"
    assert out["classification"] == "bullish"
    assert caps._directional_context_checked == [{"symbol": "SPY", "result": out}]


def test_risk_exposure_returns_actionable_structure_ids_without_aliasing():
    caps = object.__new__(Capabilities)
    caps.open_positions = [{"structure_id": "sid-1", "legs": [{"symbol": "OPT"}]}]
    caps.open_premium_at_risk = 250
    caps.realised_loss = 10
    caps.equity = 99_990

    exposure = caps._risk_exposure()
    structures = caps._risk_structures()
    exposure["structures"][0]["structure_id"] = "mutated"

    assert structures[0]["structure_id"] == "sid-1"
    assert caps.open_positions[0]["structure_id"] == "sid-1"


def test_model_can_delegate_a_validated_trailing_exit(tmp_path):
    caps = object.__new__(Capabilities)
    caps.open_positions = [{"structure_id": "sid-1", "cost_basis": -200,
                            "profit_target": 100}]
    caps.exit_policies = ExitPolicyStore(tmp_path / "exit_policies.jsonl")
    caps.params = type("Params", (), {"profit_target_pct": 50.0})()

    got = caps._trading_set_exit_policy(
        "sid-1", 40, 15, 20, 2, "protect a real executable gain")

    assert got["activation_profit"] == 40
    assert got["current_trigger_profit"] == 20
    with pytest.raises(CapabilityError, match="unknown open structure"):
        caps._trading_set_exit_policy("invented", 40, 15, 20, 2, "invalid")


def test_model_can_arm_list_and_remove_exact_exit_trigger(tmp_path):
    caps = object.__new__(Capabilities)
    caps.open_positions = [{"structure_id": "sid-1"}]
    caps.action_triggers = ActionTriggerStore(tmp_path / "action_triggers.jsonl")
    caps._submitted_this_program = False
    caps._trading_result = None

    armed = caps._trading_set_exit_trigger(
        "sid-1", 35, valid_for_seconds=60, reason="take a real executable gain")

    assert armed["status"] == "trigger_armed"
    assert armed["condition"] == {"kind": "min_executable_profit", "value": 35.0}
    assert caps._trading_list_triggers()[0]["trigger_id"] == armed["trigger_id"]
    removed = caps._trading_remove_trigger(armed["trigger_id"], "news changed premise")
    assert removed["status"] == "trigger_removed"
    assert removed["trigger_status"] == "cancelled"
    assert caps._trading_list_triggers() == []


class ControlExecutor:
    def __init__(self, staged=None):
        self.latest_staged = staged
        self.discarded = False

    def discard_staged(self):
        self.latest_staged = None
        self.discarded = True


def control_caps(staged=None):
    caps = object.__new__(Capabilities)
    caps.ex = ControlExecutor(staged)
    caps._program_decision = None
    caps._submitted_this_program = False
    return caps


def test_terminal_decision_is_structured_and_program_scoped():
    caps = control_caps()
    caps.begin_program()
    assert caps._decision_no_trade("simulation rejected") == {
        "status": "no_trade", "reason": "simulation rejected",
        "discarded_staged": False}
    assert caps.program_decision == {"status": "no_trade",
                                     "reason": "simulation rejected"}
    with pytest.raises(CapabilityError, match="already ended"):
        caps._decision_no_trade("again")
    caps.begin_program()
    assert caps.program_decision is None


def test_no_trade_discards_a_passing_unsubmitted_draft():
    caps = control_caps(staged=object())
    out = caps._decision_no_trade("simulation invalidated the edge")
    assert out["status"] == "no_trade" and out["discarded_staged"] is True
    assert caps.ex.discarded and caps.ex.latest_staged is None


def test_invalid_no_trade_reason_does_not_discard_the_draft():
    staged = object()
    caps = control_caps(staged=staged)
    with pytest.raises(CapabilityError, match="reason must be non-empty"):
        caps._decision_no_trade("  ")
    assert caps.ex.latest_staged is staged and not caps.ex.discarded


def test_trade_cannot_follow_a_terminal_program_decision():
    caps = control_caps()
    caps._decision_no_trade("no edge")
    with pytest.raises(CapabilityError, match="do not trade after"):
        caps._trading_execute({})


def test_model_can_only_close_an_exact_reconciled_structure():
    structure = {"structure_id": "spread-1", "qty": 2}

    class ClosingExecutor(ControlExecutor):
        def close_structure(self, got, reason, now, **kwargs):
            assert got is structure and reason == "thesis invalidated"
            assert kwargs["must_fill"] is False
            return {"status": "submitted_close", "structure_id": "spread-1"}

    caps = object.__new__(Capabilities)
    caps.ex = ClosingExecutor()
    caps.open_positions = [structure]
    caps._program_decision = None
    caps._submitted_this_program = False
    caps._trading_result = None

    with pytest.raises(CapabilityError, match="unknown open structure"):
        caps._trading_close("spread-2", "thesis invalidated")

    out = caps._trading_close("spread-1", "thesis invalidated")
    assert out["status"] == "submitted_close"
    assert caps.trading_result == out
    with pytest.raises(CapabilityError, match="already submitted"):
        caps._trading_close("spread-1", "again")


def test_scenario_breach_close_is_durable_and_thesis_close_defers():
    structure = {"structure_id": "spread-1", "thesis_id": "th-1", "qty": 2}

    class ClosingExecutor(ControlExecutor):
        def close_structure(self, got, reason, now, **kwargs):
            assert got is structure
            assert kwargs == {"must_fill": True,
                              "mandatory_source": "portfolio_scenario_breach"}
            return {"status": "submitted_close", "structure_id": "spread-1"}

    caps = object.__new__(Capabilities)
    caps.ex = ClosingExecutor()
    caps.open_positions = [structure]
    caps.trigger = {"name": "portfolio_scenario_breach"}
    caps._program_decision = None
    caps._submitted_this_program = False
    caps._trading_result = None

    out = caps._trading_close("spread-1", "repair the correlated book")
    deferred = caps._thesis_close("th-1", "submitted close", realised=-20)

    assert out["status"] == "submitted_close"
    assert deferred["status"] == "deferred_until_flat"
