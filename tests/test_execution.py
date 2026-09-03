import datetime as dt
from dataclasses import replace
import httpx
import pytest
from agent.config import ET
from agent.host.capabilities import Capabilities, CapabilityError
from agent.host.contracts import parse_occ_symbol
from agent.host.execution import Executor
from agent.host.ledger import ExecutionLedger
from agent.host.risk_params import DEFAULT as RP
from agent.host.thesis_store import ThesisStore
from agent.quant.candidates import Candidate
from agent.types import Leg, TradeIntent

EXP = dt.date(2026, 9, 3)
NOW = dt.datetime(2026, 9, 1, 15, 0, tzinfo=dt.timezone.utc)
EXPECTED_ACCOUNT_ID = "test-competition-account"
ACCOUNT = {"id": EXPECTED_ACCOUNT_ID, "equity": "100000", "options_trading_level": 3,
           "options_buying_power": "100000", "trading_blocked": False,
           "account_blocked": False}


def leg(strike, kind, side):
    intent = "buy_to_open" if side == "buy" else "sell_to_open"
    return Leg(f"SPY260903{kind[0].upper()}{strike*1000:08.0f}", 1, side, intent,
               strike, kind, EXP)


class FakeRest:
    """Quotes and account only -- the executor must not need anything else to price."""
    profile = "dev"

    def __init__(self, quotes, account=None):
        self.quotes, self._account = quotes, account or ACCOUNT
        self.submitted = []

    def option_quotes(self, symbols):
        ts = NOW.isoformat().replace("+00:00", "Z")
        return {s: {**self.quotes[s], "t": ts} for s in symbols if s in self.quotes}

    def account(self):
        return self._account

    def option_contract(self, symbol):
        meta = parse_occ_symbol(symbol)
        return {"symbol": meta.symbol, "underlying_symbol": meta.underlying,
                "strike_price": str(meta.strike), "type": meta.option_type,
                "expiration_date": meta.expiry.isoformat(), "tradable": True,
                "status": "active"}

    def submit_mleg(self, legs, qty, limit_price, coid, tif="day"):
        self.submitted.append((legs, qty, limit_price, coid))
        return {"id": "ord-1"}

    def submit_single(self, *a, **k):
        self.submitted.append(a)
        return {"id": "ord-1"}


class AmbiguousRest(FakeRest):
    def __init__(self, quotes=None, account=None):
        super().__init__(quotes or GOOD_QUOTES, account)
        self.accepted = {}
        self.lookup_override = None

    def submit_mleg(self, legs, qty, limit_price, coid, tif="day"):
        request = {"order_class": "mleg", "qty": str(qty), "type": "limit",
                   "limit_price": f"{limit_price:.2f}", "time_in_force": tif,
                   "client_order_id": coid, "legs": legs}
        self.submitted.append((legs, qty, limit_price, coid))
        self.accepted[coid] = {**request, "id": "accepted-1", "status": "new",
                               "filled_qty": "0", "filled_avg_price": None}
        raise TimeoutError("response lost after broker acceptance")

    def order_by_client_order_id(self, coid):
        return dict(self.lookup_override or self.accepted[coid])


def vertical(risk_budget=5000.0):
    return TradeIntent("SPY", "vertical_call",
                       (leg(770, "call", "buy"), leg(775, "call", "sell")),
                       "th_test", risk_budget)


def credit_vertical(risk_budget=5000.0):
    return TradeIntent("SPY", "vertical_call",
                       (leg(770, "call", "sell"), leg(775, "call", "buy")),
                       "th_test", risk_budget)


def intent_json(intent=None):
    intent = intent or vertical()
    return {"underlying": intent.underlying, "family": intent.family,
            "legs": Executor._legs_json(intent), "thesis_id": intent.thesis_id,
            "risk_budget": intent.risk_budget}


GOOD_QUOTES = {
    "SPY260903C00770000": {"bp": 4.00, "ap": 4.10},
    "SPY260903C00775000": {"bp": 1.40, "ap": 1.50},
}


def make(quotes=None, mode="execute", account=None):
    rest = FakeRest(quotes or GOOD_QUOTES, account)
    return Executor(rest, RP, "competition", mode=mode,
                    expected_account_id=EXPECTED_ACCOUNT_ID), rest


def evidence(edges=(0.10, 0.08, 0.05), stable=True):
    return {
        "evaluation": {"candidate": "candidate-1", "edge_by_measure": {
            "lognormal": edges[0], "block_bootstrap": edges[1],
            "student_t": edges[2]}, "evaluated_net_price": 2.70,
            "expected_profit_by_measure": {
                "lognormal": 55.0, "block_bootstrap": 45.0,
                "student_t": 35.0}},
        "ranking": {"stable_top": ["candidate-1"] if stable else []},
        "direction": {"sigma": 0.15, "days": 2},
    }


def enforced(edges=(0.10, 0.08, 0.05), stable=True):
    rest = FakeRest(GOOD_QUOTES)
    ex = Executor(rest, RP, "competition", mode="execute",
                  expected_account_id=EXPECTED_ACCOUNT_ID,
                  enforce_entry_risk=True)
    staged = ex.materialise(
        vertical(), equity=100_000, now=NOW,
        entry_evidence=evidence(edges, stable), market_spots={"SPY": 772})
    return staged, ex, rest


def test_price_comes_from_quotes_not_the_model():
    ex, _ = make()
    staged = ex.materialise(vertical(), equity=100_000, now=NOW)
    # buy the ask 4.10, sell the bid 1.40 -> net debit 2.70
    assert staged.verified.limit_price == pytest.approx(2.70)


def test_quantity_comes_from_the_risk_budget():
    ex, _ = make()
    staged = ex.materialise(vertical(risk_budget=1350.0), equity=100_000, now=NOW)
    assert staged.verified.qty == 5           # 1350 // 270


def test_host_evidence_and_resulting_book_scenario_bound_quantity():
    staged, _, _ = enforced()
    assert staged.passed
    assert staged.verified.qty > 0
    assert staged.verified.qty < 18  # requested $5k budget cannot bypass host ceilings
    assert staged.sizing["portfolio_scenario"]["resulting_breached"] is False
    assert {row.name for row in staged.results} >= {
        "volatility_evidence", "portfolio_scenario"}


def test_fresh_price_repricing_refuses_when_weakest_edge_falls_below_live_friction():
    rest = FakeRest(GOOD_QUOTES)
    ex = Executor(rest, RP, "competition", mode="execute",
                  expected_account_id=EXPECTED_ACCOUNT_ID,
                  enforce_entry_risk=True)
    stale = evidence()
    stale["evaluation"]["evaluated_net_price"] = 2.60
    staged = ex.materialise(
        vertical(), equity=100_000, now=NOW,
        entry_evidence=stale, market_spots={"SPY": 772})
    gate = next(row for row in staged.results if row.name == "fresh_price_edge")
    assert gate.passed is False
    assert staged.sizing["fresh_price_edge"]["weakest_expected_profit"] == 25.0
    assert staged.sizing["fresh_price_edge"]["required_expected_profit"] == 30.0


def terminal_params():
    return replace(
        RP,
        robust_evidence_risk_pct=25.0,
        max_correlated_scenario_loss_pct=25.0,
        max_single_position_pct=25.0,
        max_total_premium_at_risk_pct=60.0,
        max_aligned_direction_risk_pct=25.0,
    )


def terminal_executor(*, ledger=None, posture="terminal_push"):
    rest = FakeRest(GOOD_QUOTES)
    ex = Executor(
        rest, terminal_params(), "competition", mode="execute", ledger=ledger,
        expected_account_id=EXPECTED_ACCOUNT_ID, enforce_entry_risk=True,
        sizing_posture=posture,
    )
    ex.begin_cycle("terminal-cycle")
    ex.begin_program(1)
    return ex, rest


def test_terminal_push_reconsiders_materially_undersized_excellent_entry():
    ex, rest = terminal_executor()

    out = ex.execute(
        vertical(5_000), equity=100_000, now=NOW,
        entry_evidence=evidence(), market_spots={"SPY": 772})

    assert out["status"] == "reconsider_sizing"
    assert out["target_risk_pct"] == 20.0
    assert out["target_qty"] == 74
    assert out["requested_qty"] == 18
    assert out["available_qty"] >= out["target_qty"]
    assert ex.latest_staged is None
    assert rest.submitted == []


def test_terminal_push_accepts_target_sized_excellent_entry_for_review():
    ex, rest = terminal_executor()

    out = ex.execute(
        vertical(20_000), equity=100_000, now=NOW,
        entry_evidence=evidence(), market_spots={"SPY": 772})

    assert out["status"] == "staged"
    assert out["qty"] == 74
    assert out["passed"] is True
    assert rest.submitted == []


def test_terminal_push_does_not_fuss_over_a_near_target_request():
    ex, rest = terminal_executor()

    out = ex.execute(
        vertical(18_000), equity=100_000, now=NOW,
        entry_evidence=evidence(), market_spots={"SPY": 772})

    assert out["status"] == "staged"
    assert out["qty"] == 66
    assert rest.submitted == []


def test_scaled_balanced_never_receives_terminal_undersizing_coercion():
    ex, rest = terminal_executor(posture="scaled_balanced")

    out = ex.execute(
        vertical(5_000), equity=100_000, now=NOW,
        entry_evidence=evidence(), market_spots={"SPY": 772})

    assert out["status"] == "staged"
    assert out["qty"] == 18
    assert "terminal_push_target" not in out["sizing"]
    assert rest.submitted == []


def test_terminal_push_is_durable_and_follow_on_robust_entries_revert_to_four_pct(
        tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    ledger.prepare_submission(
        client_order_id="x-terminal-used", request={}, structure_id="s-used",
        purpose="entry", thesis_id="th-used", underlying="SPY",
        family="vertical_call", legs=[], qty=1, signed_limit_price=1.0,
        max_loss_per_unit=1.0, cycle_id="earlier-cycle",
        sizing_posture="terminal_push")
    ex, _ = terminal_executor(ledger=ledger)

    staged = ex.materialise(
        vertical(20_000), equity=100_000, now=NOW,
        entry_evidence=evidence(), market_spots={"SPY": 772})

    assert staged.verified.qty == 14
    assert staged.sizing["headroom_qty"]["volatility_evidence"] == 14
    assert "terminal_push_target" not in staged.sizing
    evidence_gate = next(
        row for row in staged.results if row.name == "volatility_evidence")
    assert "ceiling $4,000" in evidence_gate.reason


def test_count_cap_exemption_uses_positive_binding_scenario_effect_not_family_label(
        monkeypatch):
    from agent.host import portfolio_risk

    contribution = {"value": 25.0}

    def stress(*args, **kwargs):
        return {
            "status": "ok", "missing_symbols": [],
            "worst_current": {"spot_expected_move_multiple": -1.0,
                              "iv_relative_shock": 0.0,
                              "current_book_pnl": -100.0, "spots": {}},
            "scenarios": [{"spot_expected_move_multiple": -1.0,
                           "iv_relative_shock": 0.0,
                           "current_book_pnl": -100.0,
                           "candidate_unit_pnl": contribution["value"]}],
        }

    def assess(stress_result, equity, limit_pct, qty):
        return {"status": "ok", "allowed_qty": qty, "current_breached": False,
                "resulting_breached": False, "current_worst_pnl": -100,
                "resulting_worst_pnl": -75, "loss_limit_dollars": 4000}

    monkeypatch.setattr(portfolio_risk, "stress_portfolio", stress)
    monkeypatch.setattr(portfolio_risk, "assess_admission", assess)
    open_book = [{"underlying": "SPY", "family": "iron_condor",
                  "premium_type": "short", "legs": []} for _ in range(8)]
    rest = FakeRest(GOOD_QUOTES)
    ex = Executor(rest, RP, "competition", mode="execute",
                  expected_account_id=EXPECTED_ACCOUNT_ID,
                  enforce_entry_risk=True)

    helpful = ex.materialise(
        vertical(), equity=100_000, now=NOW, open_positions=open_book,
        entry_evidence=evidence(), market_spots={"SPY": 772})
    assert next(row for row in helpful.results if row.name == "concentration").passed
    assert helpful.sizing["portfolio_scenario"]["measured_scenario_reducing"] is True

    contribution["value"] = -25.0
    harmful = ex.materialise(
        vertical(), equity=100_000, now=NOW, open_positions=open_book,
        entry_evidence=evidence(), market_spots={"SPY": 772})
    assert not next(row for row in harmful.results if row.name == "concentration").passed


def test_partial_evidence_gets_half_percent_ceiling_and_weak_evidence_refuses():
    partial, _, _ = enforced(edges=(.10, .05, -.01), stable=False)
    weak, _, _ = enforced(edges=(.10, -.20, -.10), stable=True)
    assert partial.sizing["headroom_qty"]["volatility_evidence"] == 1
    assert partial.verified.qty <= 1
    assert weak.verified.qty == 0
    assert not weak.passed
    assert next(row for row in weak.results
                if row.name == "volatility_evidence").passed is False


def test_upcoming_scheduled_event_halves_new_short_gamma_evidence_ceiling():
    rest = FakeRest(GOOD_QUOTES)
    ex = Executor(rest, RP, "competition", mode="execute",
                  expected_account_id=EXPECTED_ACCOUNT_ID,
                  enforce_entry_risk=True)
    event_evidence = evidence()
    event_evidence["scheduled_events"] = {"next_event": {
        "name": "macro release", "at_et": "2026-09-01T12:00:00-04:00",
        "minutes_until": 60,
    }}

    staged = ex.materialise(
        credit_vertical(), equity=100_000, now=NOW,
        entry_evidence=event_evidence, market_spots={"SPY": 772})

    # $250 max loss per unit; robust $4k ceiling becomes $2k near the event.
    assert staged.sizing["headroom_qty"]["volatility_evidence"] == 8
    evidence_gate = next(row for row in staged.results
                         if row.name == "volatility_evidence")
    assert "multiplier 0.50" in evidence_gate.reason


def test_scheduled_event_discount_does_not_obstruct_breached_book_repair():
    rest = FakeRest(GOOD_QUOTES)
    ex = Executor(rest, RP, "competition", mode="execute",
                  expected_account_id=EXPECTED_ACCOUNT_ID,
                  enforce_entry_risk=True)
    event_evidence = evidence()
    event_evidence.update({
        "scheduled_events": {"next_event": {"minutes_until": 30}},
        "current_scenario_breached": True,
    })

    staged = ex.materialise(
        credit_vertical(), equity=100_000, now=NOW,
        entry_evidence=event_evidence, market_spots={"SPY": 772})

    assert staged.sizing["headroom_qty"]["volatility_evidence"] == 16
    evidence_gate = next(row for row in staged.results
                         if row.name == "volatility_evidence")
    assert "multiplier 1.00" in evidence_gate.reason


def test_single_position_cap_bounds_quantity():
    ex, _ = make()
    staged = ex.materialise(vertical(risk_budget=90_000.0), equity=100_000, now=NOW)
    assert staged.verified.qty == 14          # 4% of equity // 270


def test_portfolio_headroom_downsizes_and_recomputes_final_economics():
    ex, _ = make()
    staged = ex.materialise(
        vertical(risk_budget=5000), equity=100_000,
        open_premium_at_risk=14_460, now=NOW)
    assert staged.sizing["requested_qty"] == 18
    assert staged.sizing["binding_constraint"] == "portfolio"
    assert staged.verified.qty == 2
    assert staged.verified.max_loss == pytest.approx(540)
    assert staged.verified.max_profit == pytest.approx(460)
    assert staged.passed


def test_buying_power_can_be_the_binding_quantity():
    account = {**ACCOUNT, "options_buying_power": "300"}
    ex, _ = make(account=account)
    staged = ex.materialise(vertical(), equity=100_000, now=NOW)
    assert staged.verified.qty == 1
    assert staged.sizing["binding_constraint"] == "buying_power"


def test_zero_headroom_never_gets_a_one_lot_floor():
    ex, _ = make()
    staged = ex.materialise(vertical(), equity=100_000,
                            open_premium_at_risk=40_000, now=NOW)
    assert staged.verified.qty == 0
    assert staged.sizing["allowed_qty"] == 0
    assert not staged.passed


def test_zero_bid_leg_blocks():
    q = {**GOOD_QUOTES, "SPY260903C00775000": {"bp": 0.00, "ap": 0.05}}
    ex, _ = make(q)
    staged = ex.materialise(vertical(), equity=100_000, now=NOW)
    assert not staged.passed
    assert any("no exit" in r.reason for r in staged.results)


def test_confirm_requires_prior_stage():
    ex, _ = make()
    with pytest.raises(PermissionError, match="nothing staged"):
        ex.confirm(vertical(), equity=100_000, now=NOW)


def test_two_phase_submits_only_on_confirm():
    ex, rest = make()
    ex.materialise(vertical(), equity=100_000, now=NOW)
    assert rest.submitted == []
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "submitted" and len(rest.submitted) == 1


def test_submit_boundary_refuses_entry_when_time_window_closed(monkeypatch):
    ex, rest = make()
    ex.materialise(vertical(), equity=100_000, now=NOW)
    monkeypatch.setattr(
        "agent.host.execution.entry_submission_allowed",
        lambda _now=None: (False, "new entries closed at 15:55 ET"),
    )
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "entry_window_closed"
    assert rest.submitted == []


def test_submit_boundary_uses_fresh_clock_not_cycle_time():
    ex, rest = make()
    ex.submission_clock = lambda: dt.datetime(2026, 9, 3, 15, 55, tzinfo=ET)
    ex.materialise(vertical(), equity=100_000, now=NOW)
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "entry_window_closed"
    assert rest.submitted == []


def test_confirmation_reprices_and_can_only_reduce_the_reviewed_quantity():
    ex, rest = make()
    staged = ex.materialise(vertical(risk_budget=1350), equity=100_000, now=NOW)
    assert staged.verified.qty == 5
    rest.quotes = {**rest.quotes,
                   "SPY260903C00770000": {"bp": 4.40, "ap": 4.50}}
    out = ex.confirm(vertical(risk_budget=1350), equity=100_000, now=NOW)
    assert out["status"] == "submitted"
    assert out["qty"] == 4
    assert out["limit_price"] == pytest.approx(3.10)


def test_confirmation_never_increases_above_the_staged_quantity():
    expensive = {**GOOD_QUOTES,
                 "SPY260903C00770000": {"bp": 4.40, "ap": 4.50}}
    ex, rest = make(expensive)
    staged = ex.materialise(vertical(risk_budget=1350), equity=100_000, now=NOW)
    assert staged.verified.qty == 4
    rest.quotes = {**rest.quotes,
                   "SPY260903C00770000": {"bp": 4.00, "ap": 4.10}}
    out = ex.confirm(vertical(risk_budget=1350), equity=100_000, now=NOW)
    assert out["qty"] == 4


def test_nonce_prevents_replay():
    ex, rest = make()
    ex.materialise(vertical(), equity=100_000, now=NOW)
    ex.confirm(vertical(), equity=100_000, now=NOW)
    with pytest.raises(PermissionError, match="already executed"):
        ex.confirm(vertical(), equity=100_000, now=NOW)
    assert len(rest.submitted) == 1


def test_expired_intent_restages_rather_than_submitting():
    ex, rest = make()
    staged = ex.materialise(vertical(), equity=100_000, now=NOW)
    object.__setattr__(staged.verified, "ttl_seconds", -1.0)
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "restaged" and rest.submitted == []


def test_propose_mode_never_submits():
    ex, rest = make(mode="propose")
    ex.materialise(vertical(), equity=100_000, now=NOW)
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "proposed" and rest.submitted == []


def test_client_order_id_is_deterministic_per_nonce():
    ex, _ = make()
    s = ex.materialise(vertical(), equity=100_000, now=NOW)
    assert s.verified.client_order_id() == s.verified.client_order_id()
    assert len(s.verified.client_order_id()) == 32


def test_checklist_renders_verdict():
    ex, _ = make()
    s = ex.materialise(vertical(), equity=100_000, now=NOW)
    text = s.checklist()
    assert "max loss" in text and ("EXECUTABLE" in text or "BLOCKED" in text)


def test_fill_management_cancels_when_unfilled():
    ex, rest = make()
    rest.order = lambda oid: {"status": "new", "filled_qty": "0"}
    rest.cancel = lambda oid: rest.__dict__.setdefault("cancelled", []).append(oid)
    out = ex.manage_fill("ord-1", steps=2, step_seconds=0, sleep=lambda s: None)
    assert out["status"] == "cancelled_unfilled" and rest.cancelled == ["ord-1"]


def test_fill_management_reports_fill():
    ex, rest = make()
    rest.order = lambda oid: {"status": "filled", "filled_avg_price": "2.68"}
    out = ex.manage_fill("ord-1", steps=3, step_seconds=0, sleep=lambda s: None)
    assert out["status"] == "filled" and out["price"] == "2.68"


def test_staging_is_cycle_scoped_and_full_intent_is_hashed():
    ex, rest = make()
    ex.begin_cycle("cycle-1")
    first = ex.execute(vertical(), equity=100_000, now=NOW)
    assert first["status"] == "staged" and rest.submitted == []
    assert ex._key(vertical()) != ex._key(vertical(risk_budget=4999.0))
    ex.end_cycle()
    ex.begin_cycle("cycle-2")
    again = ex.execute(vertical(), equity=100_000, now=NOW)
    assert again["status"] == "staged" and rest.submitted == []


def test_same_model_program_cannot_confirm():
    ex, rest = make()
    ex.begin_cycle("cycle-1")
    ex.begin_program(1)
    first = ex.execute(vertical(), equity=100_000, now=NOW)
    second = ex.execute(vertical(), equity=100_000, now=NOW)
    assert first["status"] == "staged"
    assert second["status"] == "awaiting_confirmation"
    assert rest.submitted == []


def test_later_model_program_can_confirm_identical_intent():
    ex, rest = make()
    ex.begin_cycle("cycle-1")
    ex.begin_program(1)
    ex.execute(vertical(), equity=100_000, now=NOW)
    ex.begin_program(2)
    out = ex.execute(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "submitted"
    assert len(rest.submitted) == 1


def test_failed_program_can_discard_its_unsubmitted_draft():
    ex, rest = make()
    ex.begin_cycle("cycle-1")
    ex.begin_program(1)
    ex.execute(vertical(), equity=100_000, now=NOW)
    ex.discard_staged()
    ex.begin_program(2)
    out = ex.execute(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "staged"
    assert rest.submitted == []


def test_expired_draft_restaged_in_later_program_needs_another_program():
    ex, rest = make()
    ex.begin_cycle("cycle-1")
    ex.begin_program(1)
    ex.execute(vertical(), equity=100_000, now=NOW)
    object.__setattr__(ex.latest_staged.verified, "ttl_seconds", -1.0)
    ex.begin_program(2)
    restaged = ex.execute(vertical(), equity=100_000, now=NOW)
    repeated = ex.execute(vertical(), equity=100_000, now=NOW)
    assert restaged["status"] == "restaged"
    assert repeated["status"] == "awaiting_confirmation"
    assert rest.submitted == []


def test_quote_ttl_lapse_reprices_inside_live_economic_authorization():
    ex, rest = make()
    condition = {"kind": "max_entry_debit", "value": 3.00}
    ex.begin_cycle("cycle-1")
    ex.begin_program(1)
    first = ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=120,
        equity=100_000, now=NOW)
    assert first["status"] == "staged"
    assert first["confirmation_call"] == {
        "namespace": "trading",
        "function": "execute_if",
        "intent": "identical canonical_staged_order",
        "kwargs": {"max_entry_debit": 3.0, "valid_for_seconds": 120},
        "authorization_deadline": "2026-09-01T15:02:00+00:00",
        "warning": (
            "repeat execute_if with the identical intent and boundary; "
            "switching to trading.execute cannot confirm this draft"),
    }
    object.__setattr__(ex.latest_staged.verified, "ttl_seconds", -1.0)

    ex.begin_program(2)
    confirmed = ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=120,
        equity=100_000, now=NOW)

    assert confirmed["status"] == "submitted"
    assert len(rest.submitted) == 1


def test_execute_if_rechecks_canonical_signal_policy_before_submit():
    ex, rest = make()
    condition = {"kind": "max_entry_debit", "value": 3.0}
    policy = {
        "schema_version": 1, "mode": "momentum_continuation",
        "requested_mode": "auto", "candidate_bias": "bullish",
        "directionality": "direction-led", "analysis_classification": "bullish",
        "reference_spot": 772.0, "expected_move": 6.0,
        "max_adverse_move_em": 0.15, "confirmation_samples": 2,
        "sample_interval_seconds": 1.0,
    }
    ex.begin_cycle("cycle-signal")
    ex.begin_program(1)
    staged = ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=30,
        signal_policy=policy,
        signal_verdict={"status": "passed"}, equity=100_000, now=NOW)
    assert staged["status"] == "staged"
    assert staged["confirmation_call"]["kwargs"]["entry_mode"] == "auto"

    ex.begin_program(2)
    waiting = ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=30,
        signal_policy=policy,
        signal_verdict={"status": "waiting_signal",
                        "reason": "current label is neutral"},
        equity=100_000, now=NOW + dt.timedelta(seconds=1))
    assert waiting["status"] == "signal_not_met"
    assert rest.submitted == []
    assert ex.latest_staged is not None

    ex.begin_program(3)
    submitted = ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=30,
        signal_policy=policy,
        signal_verdict={"status": "passed", "reason": "bullish remains aligned"},
        equity=100_000, now=NOW + dt.timedelta(seconds=2))
    assert submitted["status"] == "submitted"
    assert len(rest.submitted) == 1


def test_changed_signal_policy_cannot_confirm_an_existing_draft():
    ex, rest = make()
    condition = {"kind": "max_entry_debit", "value": 3.0}
    momentum = {"mode": "momentum_continuation", "candidate_bias": "bullish"}
    pullback = {"mode": "pullback_entry", "candidate_bias": "bullish"}
    ex.begin_cycle("cycle-signal-hash")
    ex.begin_program(1)
    assert ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=30,
        signal_policy=momentum, signal_verdict={"status": "passed"},
        equity=100_000, now=NOW)["status"] == "staged"

    ex.begin_program(2)
    changed = ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=30,
        signal_policy=pullback, signal_verdict={"status": "passed"},
        equity=100_000, now=NOW + dt.timedelta(seconds=1))
    assert changed["status"] == "staged"
    assert rest.submitted == []
    assert ex.latest_staged.signal_policy == pullback


def test_execute_if_uses_post_materialisation_signal_callback():
    ex, rest = make()
    condition = {"kind": "max_entry_debit", "value": 3.0}
    policy = {"mode": "momentum_continuation", "candidate_bias": "bullish"}
    calls = []

    def current_signal():
        calls.append("checked")
        return {"status": "waiting_signal", "reason": "move reversed during refresh"}

    ex.begin_cycle("cycle-late-signal")
    ex.begin_program(1)
    assert ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=30,
        signal_policy=policy, signal_verdict={"status": "passed"},
        signal_revalidator=current_signal,
        equity=100_000, now=NOW)["status"] == "staged"
    assert calls == [], "the fire-time callback is not part of draft construction"

    ex.begin_program(2)
    out = ex.execute(
        vertical(), economic_condition=condition, authorization_seconds=30,
        signal_policy=policy, signal_verdict={"status": "passed"},
        signal_revalidator=current_signal,
        equity=100_000, now=NOW + dt.timedelta(seconds=1))
    assert out["status"] == "signal_not_met"
    assert out["signal_verdict"]["reason"] == "move reversed during refresh"
    assert calls == ["checked"]
    assert rest.submitted == []


def test_changed_intent_replaces_the_only_cycle_draft():
    ex, _ = make()
    ex.begin_cycle("cycle-1")
    ex.execute(vertical(), equity=100_000, now=NOW)
    changed = vertical(risk_budget=1350)
    out = ex.execute(changed, equity=100_000, now=NOW)
    assert out["status"] == "staged"
    assert list(ex._staged) == [ex._key(changed)]


def test_preview_materialisation_does_not_create_confirmation_state():
    ex, _ = make()
    ex.materialise(vertical(), equity=100_000, now=NOW, store=False)
    assert ex.latest_staged is None


def test_capability_rejects_missing_thesis_before_staging(tmp_path):
    ex, rest = make()
    caps = Capabilities(rest, object(), ThesisStore(tmp_path / "theses.jsonl"),
                        ex, RP, equity=100_000)
    with pytest.raises(CapabilityError, match="call thesis.open"):
        caps.dispatch("trading", "execute", [intent_json()], {})
    assert ex.latest_staged is None


def audited_caps(tmp_path, *, trigger="session_anchor"):
    ex, rest = make()
    ex.begin_cycle("cycle-1")
    theses = ThesisStore(tmp_path / "theses.jsonl")
    thesis = theses.open(
        "test", "SPY", exit_profit="Close at $115 profit per spread (50% max profit)",
        exit_invalidation="Long premium; no drawdown stop; volatility regime reverses",
        exit_time="2026-09-03 15:45 ET",
        exit_news="Unexpected macro news changes the distribution")
    intent = vertical()
    intent = TradeIntent(intent.underlying, intent.family, intent.legs,
                         thesis.thesis_id, intent.risk_budget)
    candidate = Candidate("SPY:cv-test", intent.family, intent.underlying,
                          EXP.isoformat(), list(intent.legs), 2.7, 270.0, 230.0,
                          5.0, 1.0)
    thesis.evidence_refs.append(candidate.id)
    class Series:
        def last(self, _symbol):
            return 772.0

        def directional_contexts(self, symbols, _now=None):
            return {str(symbol).upper(): {
                "classification": "bullish", "strength": "strong",
            } for symbol in symbols}

    caps = Capabilities(rest, Series(), theses, ex, RP, equity=100_000,
                        trigger={"name": trigger})
    caps._candidates[candidate.id] = candidate
    signature = caps._candidate_signature(candidate)
    caps._enumerated.add(signature)
    caps._directional_context_checked.append({
        "symbol": "SPY",
        "result": {"classification": "neutral", "strength": "weak"},
    })
    return caps, ex, intent, candidate, signature


def test_presubmit_hooks_return_repairable_missing_evidence(tmp_path):
    caps, ex, intent, candidate, _ = audited_caps(tmp_path)
    caps._directional_context_checked.clear()

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_evidence"
    assert out["candidate"] == candidate.id
    assert any("vol.evaluate" in item for item in out["missing"])
    assert any("vol.rank" in item for item in out["missing"])
    assert any("risk.direction" in item for item in out["missing"])
    assert any("market.directional_context" in item for item in out["missing"])
    assert ex.latest_staged is None, "missing evidence grants a repair round, not staging"


def test_lag_aware_entry_uses_the_same_presubmit_hooks(tmp_path):
    caps, ex, intent, candidate, _ = audited_caps(tmp_path)

    out = caps._trading_execute_if(
        intent_json(intent), max_entry_debit=2.75, valid_for_seconds=30)

    assert out["status"] == "needs_evidence"
    assert out["candidate"] == candidate.id
    assert any("risk.direction" in item for item in out["missing"])
    assert ex.latest_staged is None


def test_presubmit_hooks_are_bound_to_exact_candidate_and_measure(tmp_path):
    caps, ex, intent, candidate, signature = audited_caps(tmp_path)
    intent = TradeIntent(intent.underlying, intent.family, intent.legs,
                         intent.thesis_id, 3_000.0)
    caps._measure_context["m"] = {"symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "m", "result": {"edge_median": 0.1}})
    caps._ranked.append({"handle": "m", "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {"stability": 0.5}})
    caps._direction_checked.append({"candidate": candidate.id,
                                    "signature": ("wrong",),
                                    "sigma": 0.1, "days": 2.0, "result": {}})
    assert caps._trading_execute(intent_json(intent))["status"] == "needs_evidence"
    assert ex.latest_staged is None

    caps._direction_checked.append({"candidate": candidate.id,
                                    "signature": signature,
                                    "sigma": 0.1, "days": 2.0,
                                    "result": {
                                        "pnl_if_expired_now": 1,
                                        "spot": 772.0, "expected_move": 6.88,
                                        "candidate_bias": "bullish",
                                        "directionality": "direction-led",
                                        "directional_alignment": "aligned",
                                        "market_direction": {
                                            "classification": "bullish",
                                            "strength": "strong"},
                                    }})
    out = caps._trading_execute(intent_json(intent))
    assert out["status"] == "needs_price_authorization"
    assert ex.latest_staged is None

    out = caps._trading_execute_if(intent_json(intent), max_entry_debit=2.75)
    assert out["status"] == "staged"
    assert ex.latest_staged is not None
    policy = caps.theses.get(intent.thesis_id).enforced_exit_policy
    assert policy["candidate_id"] == candidate.id
    assert policy["premium_type"] == "long"
    assert policy["drawdown_stop"] is None


def test_short_premium_policy_accepts_pct_wording_and_returns_structured_requirement(
        tmp_path):
    caps, _, intent, candidate, _ = audited_caps(tmp_path)
    candidate.net_price = -0.76
    candidate.max_loss = 224.0
    candidate.max_profit = 76.0
    thesis = caps.theses.get(intent.thesis_id)
    thesis.exit_profit = "Close after capturing 50 percent of entry credit"
    thesis.exit_invalidation = (
        "Close when debit to close reaches 2x the entry credit or "
        "50 pct of defined maximum loss")

    issues = caps._thesis_policy_issues(intent, candidate.id)
    policy = caps._required_exit_policy(intent, candidate.id)

    assert not any("short-premium invalidation" in issue for issue in issues)
    assert policy["premium_type"] == "short"
    assert policy["loss_stops"] == [
        {"kind": "close_debit_multiple_of_entry_credit", "value": 2.0},
        {"kind": "loss_fraction_of_defined_maximum_loss", "value": 0.5},
    ]


def test_presubmit_rejects_direction_led_candidate_against_observed_market(tmp_path):
    caps, ex, intent, candidate, signature = audited_caps(tmp_path)
    caps._measure_context["m"] = {"symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "m", "result": {"edge_median": 0.1}})
    caps._ranked.append({"handle": "m", "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {"stability": 0.5}})
    caps._direction_checked.append({
        "candidate": candidate.id, "signature": signature,
        "sigma": 0.1, "days": 2.0,
        "result": {"directionality": "direction-led",
                   "directional_alignment": "conflicted"},
    })

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_revision"
    assert any("conflicts with current" in issue for issue in out["issues"])
    assert ex.latest_staged is None


def test_presubmit_caps_unconfirmed_directional_risk_at_three_quarter_percent(tmp_path):
    caps, ex, intent, candidate, signature = audited_caps(tmp_path)
    caps._measure_context["m"] = {"symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "m", "result": {}})
    caps._ranked.append({"handle": "m", "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {}})
    caps._direction_checked.append({
        "candidate": candidate.id, "signature": signature,
        "sigma": 0.1, "days": 2.0,
        "result": {"directionality": "direction-led",
                   "directional_alignment": "neutral"},
    })

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_revision"
    assert any("0.75%" in issue for issue in out["issues"])
    assert ex.latest_staged is None


def test_aggressive_profile_raises_only_the_aligned_direction_ceiling(tmp_path):
    caps, _, intent, candidate, signature = audited_caps(tmp_path)
    caps.params = replace(RP, max_aligned_direction_risk_pct=10.0,
                          max_single_position_pct=10.0)
    intent = TradeIntent(intent.underlying, intent.family, intent.legs,
                         intent.thesis_id, 9_000.0)
    caps._direction_checked.append({
        "candidate": candidate.id, "signature": signature,
        "sigma": 0.1, "days": 2.0,
        "result": {"directionality": "direction-led",
                   "directional_alignment": "aligned"},
    })

    issues = caps._thesis_policy_issues(intent, candidate.id)

    assert not any("aligned direction-led candidate must cap" in issue
                   for issue in issues)


def test_presubmit_does_not_tape_cap_genuinely_volatility_led_candidate(tmp_path):
    caps, _, intent, candidate, signature = audited_caps(tmp_path)
    caps._direction_checked.append({
        "candidate": candidate.id, "signature": signature,
        "sigma": 0.1, "days": 2.0,
        "result": {"directionality": "volatility-led",
                   "directional_alignment": "neutral"},
    })

    issues = caps._thesis_policy_issues(intent, candidate.id)

    assert not any("requested risk" in issue for issue in issues)
    assert not any("candidate must cap" in issue for issue in issues)


def test_presubmit_rank_must_use_the_candidate_evaluation_handle(tmp_path):
    caps, ex, intent, candidate, signature = audited_caps(tmp_path)
    caps._measure_context["candidate-measure"] = {
        "symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._measure_context["other-measure"] = {
        "symbol": "QQQ", "sigma": 0.2, "days": 3.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "candidate-measure", "result": {}})
    caps._ranked.append({"handle": "other-measure",
                         "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {}})
    caps._direction_checked.append({"candidate": candidate.id,
                                    "signature": signature,
                                    "sigma": 0.1, "days": 2.0, "result": {}})

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_evidence"
    assert any("handle that evaluated it" in item for item in out["missing"])
    assert ex.latest_staged is None


def test_news_trigger_requires_article_review_before_staging(tmp_path):
    caps, _, intent, candidate, signature = audited_caps(
        tmp_path, trigger="relevant_news")
    caps._measure_context["m"] = {"symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "m", "result": {}})
    caps._ranked.append({"handle": "m", "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {}})
    caps._direction_checked.append({"candidate": candidate.id, "signature": signature,
                                    "sigma": 0.1, "days": 2.0, "result": {}})

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_evidence"
    assert any("market.news" in item for item in out["missing"])


def test_news_review_must_cover_candidate_underlying(tmp_path):
    caps, _, intent, candidate, signature = audited_caps(
        tmp_path, trigger="relevant_news")
    caps._measure_context["m"] = {"symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "m", "result": {}})
    caps._ranked.append({"handle": "m", "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {}})
    caps._direction_checked.append({"candidate": candidate.id, "signature": signature,
                                    "sigma": 0.1, "days": 2.0, "result": {}})
    caps._news_reviewed = True
    caps._news_queries = [{"QQQ"}]

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_evidence"
    assert any("SPY" in item for item in out["missing"])


def test_presubmit_rejects_long_premium_with_short_premium_exit_policy(tmp_path):
    caps, ex, intent, candidate, signature = audited_caps(tmp_path)
    caps._measure_context["m"] = {"symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "m", "result": {}})
    caps._ranked.append({"handle": "m", "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {}})
    caps._direction_checked.append({"candidate": candidate.id, "signature": signature,
                                    "sigma": 0.1, "days": 2.0, "result": {}})
    thesis = caps.theses.get(intent.thesis_id)
    thesis.exit_invalidation = (
        "Close when debit to close reaches 2x the entry credit or 50% max loss")

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_revision"
    assert any("net-debit" in issue for issue in out["issues"])
    assert any("no drawdown stop" in issue for issue in out["issues"])
    assert out["required_exit_policy"]["premium_type"] == "long"
    assert ex.latest_staged is None


def test_presubmit_binds_thesis_to_exact_candidate_reference(tmp_path):
    caps, ex, intent, candidate, signature = audited_caps(tmp_path)
    caps._measure_context["m"] = {"symbol": "SPY", "sigma": 0.1, "days": 2.0}
    caps._evaluated.append({"candidate": candidate.id, "signature": signature,
                            "handle": "m", "result": {}})
    caps._ranked.append({"handle": "m", "signatures": {signature, ("other",)},
                         "candidate_count": 2, "result": {}})
    caps._direction_checked.append({"candidate": candidate.id, "signature": signature,
                                    "sigma": 0.1, "days": 2.0, "result": {}})
    caps.theses.get(intent.thesis_id).evidence_refs = ["SPY:some-other-candidate"]

    out = caps._trading_execute(intent_json(intent))

    assert out["status"] == "needs_revision"
    assert any("evidence_refs" in issue for issue in out["issues"])
    assert ex.latest_staged is None


def test_presubmit_rejects_exact_duplicate_of_open_structure(tmp_path):
    caps, _, intent, candidate, _ = audited_caps(tmp_path)
    caps.open_positions = [{
        "underlying": intent.underlying,
        "family": intent.family,
        "legs": [{"symbol": leg.symbol, "ratio_qty": leg.ratio_qty,
                  "side": leg.side, "position_intent": leg.position_intent}
                 for leg in intent.legs],
    }]

    issues = caps._thesis_policy_issues(intent, candidate.id)

    assert any("already open" in issue for issue in issues)


def test_preview_returns_complete_documented_economics(tmp_path):
    ex, rest = make()
    caps = Capabilities(rest, object(), ThesisStore(tmp_path / "theses.jsonl"),
                        ex, RP, equity=100_000)
    out = caps.dispatch("trading", "preview", [intent_json()], {})
    assert out["max_loss"] == pytest.approx(3780)
    assert out["max_profit"] == pytest.approx(3220)
    assert out["risk_reward"] == pytest.approx(3220 / 3780)
    assert isinstance(out["passed"], bool)


def test_contract_metadata_mismatch_is_refused_before_pricing():
    ex, _ = make()
    bad = vertical()
    wrong = Leg(bad.legs[0].symbol, 1, "buy", "buy_to_open", 771, "call", EXP)
    bad = TradeIntent("SPY", bad.family, (wrong, bad.legs[1]), bad.thesis_id,
                      bad.risk_budget)
    with pytest.raises(ValueError, match="model metadata mismatch"):
        ex.materialise(bad, equity=100_000, now=NOW)


def test_close_partial_fill_cancel_and_restart(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    ex, rest = make()
    ex.ledger = ledger
    entry = vertical(risk_budget=540)
    ledger.record_order(
        order_id="entry", client_order_id="entry-co", structure_id=ex._key(entry),
        purpose="entry", thesis_id=entry.thesis_id, underlying="SPY",
        family=entry.family, legs=ex._legs_json(entry), qty=2,
        signed_limit_price=2.70, max_loss_per_unit=270, cycle_id="cycle-entry",
        status="filled", filled_qty=2, filled_avg_price=2.70)
    positions = [
        {"asset_class": "us_option", "symbol": entry.legs[0].symbol, "qty": "2",
         "side": "long", "cost_basis": "820", "unrealized_pl": "0"},
        {"asset_class": "us_option", "symbol": entry.legs[1].symbol, "qty": "2",
         "side": "short", "cost_basis": "-280", "unrealized_pl": "0"},
    ]
    structure = ledger.risk_snapshot(positions)["structures"][0]
    out = ex.close_structure(structure, reason="test liquidation", now=NOW)
    assert out["status"] == "submitted_close"
    api_legs = rest.submitted[-1][0]
    assert [l["position_intent"] for l in api_legs] == ["buy_to_close", "sell_to_close"]
    stored_exit = ledger.descriptor_by_client_id(out["client_order_id"])
    assert [l["position_intent"] for l in stored_exit["legs"]] == [
        "buy_to_close", "sell_to_close"]

    restarted = Executor(rest, RP, "competition", mode="execute",
                         ledger=ExecutionLedger(ledger.path),
                         expected_account_id=EXPECTED_ACCOUNT_ID)
    duplicate = restarted.close_structure(structure, reason="retry", now=NOW)
    assert duplicate["status"] == "already_pending"

    rest.order = lambda oid: {"id": oid, "status": "partially_filled",
                              "filled_qty": "1", "filled_avg_price": "3.00"}
    rest.cancel = lambda oid: rest.__dict__.setdefault("cancelled", []).append(oid)
    result = restarted.manage_fill(out["order_id"], steps=1, step_seconds=0,
                                   sleep=lambda _: None)
    assert result["status"] == "cancelled_unfilled" and result["partial"] == "1"
    after = ExecutionLedger(ledger.path).risk_snapshot([
        {**positions[0], "qty": "1", "cost_basis": "410"},
        {**positions[1], "qty": "1", "cost_basis": "-140"},
    ])
    assert after["structures"][0]["qty"] == 1
    assert after["premium_at_risk"] == 270


def test_absolute_entry_cutoff_does_not_block_an_exit():
    ex, rest = make()
    intent = vertical()
    structure = {
        "structure_id": ex._key(intent),
        "thesis_id": intent.thesis_id,
        "underlying": intent.underlying,
        "family": intent.family,
        "qty": 1,
        "legs": ex._legs_json(intent),
    }
    after_window = dt.datetime(2026, 9, 4, 20, 1, tzinfo=dt.timezone.utc)

    out = ex.close_structure(structure, reason="post-window risk exit",
                             now=after_window)

    assert out["status"] == "submitted_close"
    assert len(rest.submitted) == 1


def test_timeout_after_accept_is_durable_and_adopted_by_exact_client_id(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    rest = AmbiguousRest()
    ex = Executor(rest, RP, "competition", mode="execute", ledger=ledger,
                  expected_account_id=EXPECTED_ACCOUNT_ID)
    ex.materialise(vertical(), equity=100_000, now=NOW)
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "unknown"
    coid = out["client_order_id"]
    assert ledger.execution(coid)["status"] == "unknown"
    assert ledger.descriptor_by_client_id(coid) is None

    recovered = ex.reconcile_unresolved(now=NOW)
    assert recovered[0]["order_id"] == "accepted-1"
    assert ledger.execution(coid)["status"] == "submitted"
    assert ledger.descriptor_by_client_id(coid)["order_id"] == "accepted-1"
    assert ex.entry_blockers() == []


def test_canonical_mismatch_latches_entry_freeze(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    rest = AmbiguousRest()
    ex = Executor(rest, RP, "competition", mode="execute", ledger=ledger,
                  expected_account_id=EXPECTED_ACCOUNT_ID)
    ex.materialise(vertical(), equity=100_000, now=NOW)
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    wrong = dict(rest.accepted[out["client_order_id"]])
    wrong["limit_price"] = "1.00"
    rest.lookup_override = wrong
    assert ex.reconcile_unresolved(now=NOW)[0]["status"] == "mismatch"
    assert ex.entry_blockers()[0]["status"] == "mismatch"


def test_duplicate_422_is_unknown_not_rejected(tmp_path):
    class DuplicateRest(FakeRest):
        def submit_mleg(self, *args, **kwargs):
            request = httpx.Request("POST", "https://paper-api.alpaca.markets/v2/orders")
            response = httpx.Response(
                422, request=request,
                text='{"code":42210000,"message":"client_order_id must be unique"}')
            raise httpx.HTTPStatusError("duplicate", request=request, response=response)

    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    rest = DuplicateRest(GOOD_QUOTES)
    ex = Executor(rest, RP, "competition", mode="execute", ledger=ledger,
                  expected_account_id=EXPECTED_ACCOUNT_ID)
    ex.materialise(vertical(), equity=100_000, now=NOW)
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    assert out["status"] == "unknown"
    assert ledger.execution(out["client_order_id"])["duplicate_client_order_id"] is True


def test_failed_pre_submit_fsync_prevents_broker_call(tmp_path):
    class BrokenLedger(ExecutionLedger):
        def prepare_submission(self, **kwargs):
            raise OSError("disk full")

    ledger = BrokenLedger(tmp_path / "execution.jsonl")
    ex, rest = make()
    ex.ledger = ledger
    ex.materialise(vertical(), equity=100_000, now=NOW)
    with pytest.raises(OSError, match="disk full"):
        ex.confirm(vertical(), equity=100_000, now=NOW)
    assert rest.submitted == []


def test_ambiguous_exit_blocks_only_a_second_exit_for_that_structure(tmp_path):
    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    rest = AmbiguousRest()
    ex = Executor(rest, RP, "competition", mode="execute", ledger=ledger,
                  expected_account_id=EXPECTED_ACCOUNT_ID)
    entry = vertical(risk_budget=270)
    ledger.record_order(
        order_id="entry", client_order_id="entry-co", structure_id=ex._key(entry),
        purpose="entry", thesis_id=entry.thesis_id, underlying="SPY",
        family=entry.family, legs=ex._legs_json(entry), qty=1,
        signed_limit_price=2.70, max_loss_per_unit=270, cycle_id="cycle-entry",
        status="filled", filled_qty=1, filled_avg_price=2.70)
    positions = [
        {"asset_class": "us_option", "symbol": entry.legs[0].symbol, "qty": "1",
         "side": "long", "cost_basis": "410", "unrealized_pl": "0"},
        {"asset_class": "us_option", "symbol": entry.legs[1].symbol, "qty": "1",
         "side": "short", "cost_basis": "-140", "unrealized_pl": "0"},
    ]
    structure = ledger.risk_snapshot(positions)["structures"][0]
    first = ex.close_structure(structure, reason="forced", now=NOW)
    second = ex.close_structure(structure, reason="forced retry", now=NOW)
    assert first["status"] == "unknown"
    assert second["status"] == "already_pending"
    assert second["client_order_id"] == first["client_order_id"]
    assert len(rest.submitted) == 1


def test_confirmed_absent_retry_reuses_exact_id_and_body(tmp_path):
    class MissingThenAcceptRest(FakeRest):
        def __init__(self):
            super().__init__(GOOD_QUOTES)
            self.requests = []

        def submit_mleg(self, legs, qty, limit_price, coid, tif="day"):
            self.requests.append((list(legs), qty, limit_price, coid, tif))
            if len(self.requests) == 1:
                raise TimeoutError("request may not have reached broker")
            return {"id": "retry-order", "status": "new", "filled_qty": "0"}

        def order_by_client_order_id(self, coid):
            request = httpx.Request(
                "GET", "https://paper-api.alpaca.markets/v2/orders:by_client_order_id")
            response = httpx.Response(404, request=request, text="not found")
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    rest = MissingThenAcceptRest()
    ex = Executor(rest, RP, "competition", mode="execute", ledger=ledger,
                  expected_account_id=EXPECTED_ACCOUNT_ID,
                  submission_clock=lambda: NOW)
    ex.materialise(vertical(), equity=100_000, now=NOW)
    out = ex.confirm(vertical(), equity=100_000, now=NOW)
    coid = out["client_order_id"]
    created = dt.datetime.fromisoformat(ledger.execution(coid)["created_at"])

    assert ex.reconcile_unresolved(now=created + dt.timedelta(seconds=1))[0][
        "status"] == "unknown"
    second = ex.reconcile_unresolved(now=created + dt.timedelta(seconds=16))[0]
    assert second["id"] == "retry-order"
    assert len(rest.requests) == 2
    assert rest.requests[0] == rest.requests[1]
    assert rest.requests[0][3] == coid


def test_startup_scan_latches_unknown_prefixed_broker_order(tmp_path):
    class OpenOrderRest(FakeRest):
        def orders(self, status="open"):
            assert status == "open"
            return [{"id": "broker-only", "client_order_id": "xlegacy-order"}]

    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    ex = Executor(OpenOrderRest(GOOD_QUOTES), RP, "competition", mode="execute",
                  ledger=ledger, expected_account_id=EXPECTED_ACCOUNT_ID)
    alerts = ex.scan_prefixed_open_orders()
    assert len(alerts) == 1
    assert ex.entry_blockers()[0]["status"] == "mismatch"
    assert ex.scan_prefixed_open_orders() == []  # alert is durable and not duplicated
