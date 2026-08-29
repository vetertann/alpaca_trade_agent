import datetime as dt
import pytest
from agent.config import ET, PAPER_TRADING_URL
from agent.host import gates
from agent.host.risk_params import DEFAULT as RP
from agent.types import Leg

EXP = dt.date(2026, 9, 3)
IN_WINDOW = dt.datetime(2026, 9, 1, 11, 0, tzinfo=ET)
NOW_UTC = dt.datetime(2026, 9, 1, 15, 0, tzinfo=dt.timezone.utc)


def leg(strike, kind, side, ratio=1):
    intent = "buy_to_open" if side == "buy" else "sell_to_open"
    return Leg(f"SPY260903{kind[0].upper()}{strike:08.0f}", ratio, side, intent, strike, kind, EXP)


def q(bid, ask, age_s=0.0):
    ts = (NOW_UTC - dt.timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")
    return {"bp": bid, "ap": ask, "t": ts}


EXPECTED_ACCOUNT_ID = "test-competition-account"
ACCOUNT = {"id": EXPECTED_ACCOUNT_ID, "equity": "100000", "options_trading_level": 3,
           "options_buying_power": "100000", "trading_blocked": False, "account_blocked": False}


# --- environment -------------------------------------------------------------

def test_live_endpoint_refused():
    assert not gates.g_paper_endpoint("https://api.alpaca.markets").passed
    assert gates.g_paper_endpoint(PAPER_TRADING_URL).passed


def test_dev_profile_cannot_address_competition_account():
    r = gates.g_account_identity(ACCOUNT, "dev", "test-dev-account", now=IN_WINDOW)
    assert not r.passed and "unexpected account" in r.reason


def test_missing_expected_account_id_fails_closed():
    r = gates.g_account_identity(ACCOUNT, "competition", "", now=IN_WINDOW)
    assert not r.passed and "not configured" in r.reason


def test_competition_account_blocked_outside_window():
    before = dt.datetime(2026, 8, 30, 12, 0, tzinfo=ET)
    assert not gates.g_account_identity(
        ACCOUNT, "competition", EXPECTED_ACCOUNT_ID, now=before).passed
    assert gates.g_account_identity(
        ACCOUNT, "competition", EXPECTED_ACCOUNT_ID, now=IN_WINDOW).passed


def test_blocked_account_refused():
    assert not gates.g_account_tradable({**ACCOUNT, "trading_blocked": True}).passed
    assert not gates.g_account_tradable({**ACCOUNT, "options_trading_level": 2}).passed
    assert gates.g_account_tradable(ACCOUNT).passed


# --- quote validity ----------------------------------------------------------

def test_zero_bid_refused():
    r = gates.g_quote_valid("SPY260904C00860000", q(0.00, 0.01), RP, now=NOW_UTC)
    assert not r.passed and "no exit" in r.reason


def test_crossed_quote_refused():
    assert not gates.g_quote_valid("X", q(2.10, 2.00), RP, now=NOW_UTC).passed


def test_stale_quote_refused():
    assert not gates.g_quote_valid("X", q(2.00, 2.05, age_s=600), RP, now=NOW_UTC).passed
    assert gates.g_quote_valid("X", q(2.00, 2.05, age_s=5), RP, now=NOW_UTC).passed


def test_wide_spread_refused():
    assert not gates.g_spread("X", q(0.02, 0.07), RP).passed        # 111% of mid
    assert gates.g_spread("X", q(5.07, 5.33), RP).passed            # 5.0% of mid


# --- economics ---------------------------------------------------------------

def test_dominated_spread_refused():
    """Net debit at or above the width: a loss at every outcome."""
    legs = [leg(770, "call", "buy"), leg(775, "call", "sell")]
    r = gates.g_economics(legs, net_price=5.50, qty=1, params=RP)
    assert not r.passed and "every outcome" in r.reason


def test_good_vertical_passes():
    legs = [leg(770, "call", "buy"), leg(775, "call", "sell")]
    assert gates.g_economics(legs, net_price=2.65, qty=1, params=RP).passed


def test_poor_risk_reward_refused():
    legs = [leg(770, "call", "buy"), leg(775, "call", "sell")]
    r = gates.g_economics(legs, net_price=4.50, qty=1, params=RP)   # r/r 0.11
    assert not r.passed and "risk/reward" in r.reason


def test_structure_rejects_mismatched_intent():
    bad = Leg("SPY260903C00770000", 1, "buy", "sell_to_open", 770, "call", EXP)
    assert not gates.g_structure([bad]).passed


def test_structure_rejects_five_legs():
    legs = [leg(760 + i, "call", "buy") for i in range(5)]
    assert not gates.g_structure(legs).passed


# --- portfolio ---------------------------------------------------------------

def test_single_position_cap():
    r = gates.g_risk_budget(20_000, 100_000, 0, 0, RP)   # cap is 15%
    assert not r.passed and "single-position cap" in r.reason


def test_total_premium_cap():
    r = gates.g_risk_budget(5_000, 100_000, 38_000, 0, RP)  # cap is 40%
    assert not r.passed and "at risk" in r.reason


def test_realised_loss_throttle_blocks_entry():
    r = gates.g_risk_budget(1_000, 100_000, 0, realised_loss=13_000, params=RP)
    assert not r.passed and "throttle" in r.reason
    assert "open positions untouched" in r.reason


def test_concentration_caps():
    pos = [{"underlying": "SPY"}] * 4
    assert not gates.g_concentration("SPY", pos, RP).passed
    assert gates.g_concentration("QQQ", pos, RP).passed


def test_render_shows_verdict():
    ok = [gates.g_paper_endpoint(PAPER_TRADING_URL)]
    assert "EXECUTABLE" in gates.render(ok)
    bad = [gates.g_paper_endpoint("https://api.alpaca.markets")]
    assert "BLOCKED" in gates.render(bad)


# --- spread gate: percentage with an absolute allowance ----------------------

def test_cheap_contract_passes_on_the_absolute_allowance():
    """A $0.05 spread on a $1.00 contract is 5% -- tick reality, not illiquidity."""
    r = gates.g_spread("X", q(1.00, 1.20), RP)     # 18%, $0.20
    assert r.passed and "passed on abs" in r.reason


def test_allowance_does_not_rescue_a_near_worthless_contract():
    """$0.02/$0.07 is a five-cent spread and still costs 71% of the ask to cross."""
    r = gates.g_spread("X", q(0.02, 0.07), RP)
    assert not r.passed and "neither" in r.reason


def test_expensive_contract_passes_on_percentage():
    r = gates.g_spread("X", q(5.07, 5.33), RP)     # 5.0%, $0.26
    assert r.passed and "passed on pct" in r.reason


def test_wide_on_both_measures_is_refused():
    r = gates.g_spread("X", q(1.00, 2.00), RP)     # 67%, $1.00
    assert not r.passed and "neither" in r.reason


def test_calibrated_threshold_admits_measured_atm_spreads():
    """Measured SPY/QQQ at-the-money p90 was 5.95%/3.47% at Friday's close."""
    assert gates.g_spread("SPY", q(4.52, 4.76), RP).passed      # 5.2%
    assert gates.g_spread("QQQ", q(2.16, 2.18), RP).passed      # 0.9%


# --- account identity accepts either Alpaca identifier ------------------------

ACCT_UUID = "4655c4ac-0516-42c7-95da-491e3c4e0bde"
ACCT_NUMBER = "PA3B52AVG2TD"
IDENT = {"id": ACCT_UUID, "account_number": ACCT_NUMBER, "equity": "100000",
         "options_trading_level": 3}


def test_identity_accepts_the_uuid():
    assert gates.g_account_identity(IDENT, "competition", ACCT_UUID, now=IN_WINDOW).passed


def test_identity_accepts_the_account_number_shown_in_the_dashboard():
    """The UI never shows the UUID, so the PA number is what people will configure."""
    assert gates.g_account_identity(IDENT, "competition", ACCT_NUMBER, now=IN_WINDOW).passed


def test_identity_tolerates_surrounding_whitespace():
    assert gates.g_account_identity(IDENT, "competition", f"  {ACCT_NUMBER} ",
                                    now=IN_WINDOW).passed


def test_identity_still_refuses_a_different_account():
    other = {**IDENT, "id": "130b6905-c3df-48fe-8536-432777020de2",
             "account_number": "PA3XCANY77UX"}
    r = gates.g_account_identity(other, "competition", ACCT_UUID, now=IN_WINDOW)
    assert not r.passed and "unexpected account" in r.reason


def test_identity_fails_closed_when_unconfigured():
    for missing in (None, "", "   "):
        r = gates.g_account_identity(IDENT, "competition", missing, now=IN_WINDOW)
        assert not r.passed and "not configured" in r.reason
