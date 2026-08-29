import datetime as dt
import pytest
from agent.config import ET
from agent.host.capabilities import Capabilities, CapabilityError
from agent.host.contracts import parse_occ_symbol
from agent.host.execution import Executor
from agent.host.ledger import ExecutionLedger
from agent.host.risk_params import DEFAULT as RP
from agent.host.thesis_store import ThesisStore
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


def vertical(risk_budget=5000.0):
    return TradeIntent("SPY", "vertical_call",
                       (leg(770, "call", "buy"), leg(775, "call", "sell")),
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


def test_price_comes_from_quotes_not_the_model():
    ex, _ = make()
    staged = ex.materialise(vertical(), equity=100_000, now=NOW)
    # buy the ask 4.10, sell the bid 1.40 -> net debit 2.70
    assert staged.verified.limit_price == pytest.approx(2.70)


def test_quantity_comes_from_the_risk_budget():
    ex, _ = make()
    staged = ex.materialise(vertical(risk_budget=1350.0), equity=100_000, now=NOW)
    assert staged.verified.qty == 5           # 1350 // 270


def test_single_position_cap_bounds_quantity():
    ex, _ = make()
    staged = ex.materialise(vertical(risk_budget=90_000.0), equity=100_000, now=NOW)
    assert staged.verified.qty == 55          # 15% of equity // 270


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


def test_preview_returns_complete_documented_economics(tmp_path):
    ex, rest = make()
    caps = Capabilities(rest, object(), ThesisStore(tmp_path / "theses.jsonl"),
                        ex, RP, equity=100_000)
    out = caps.dispatch("trading", "preview", [intent_json()], {})
    assert out["max_loss"] == pytest.approx(4860)
    assert out["max_profit"] == pytest.approx(4140)
    assert out["risk_reward"] == pytest.approx(4140 / 4860)
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
