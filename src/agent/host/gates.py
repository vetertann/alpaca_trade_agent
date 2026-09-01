"""Deterministic gates.

Pure functions over plain data: no network, no SDK import, no submission path
anywhere in this module. Each returns a GateResult carrying its verdict and a
human-readable reason, which is what the PASS/FAIL checklist renders.
"""
from __future__ import annotations

import datetime as dt
import math

from agent.config import PAPER_TRADING_URL, in_scored_window
from agent.host.risk_params import RiskParams
from agent.quant import structures as st
from agent.types import GateResult, Leg


# ---------------------------------------------------------------- environment

def g_paper_endpoint(trading_url: str) -> GateResult:
    ok = trading_url.rstrip("/") == PAPER_TRADING_URL
    return GateResult("paper_endpoint", ok,
                      f"{trading_url}" + ("" if ok else " is not the paper endpoint"))


def g_account_identity(account: dict, profile_name: str, expected_account_id: str,
                       now: dt.datetime | None = None) -> GateResult:
    """Require the selected credentials to resolve to the configured account.

    Alpaca identifies one account two ways: `id` is a UUID the API returns, and
    `account_number` is the `PA…` string the dashboard shows. Either is accepted,
    because the value a person can actually see is the one they will configure.
    """
    expected = str(expected_account_id or "").strip()
    if not expected:
        return GateResult("account_identity", False,
                          f"{profile_name} expected account id is not configured")
    known = {str(account.get("id", "")), str(account.get("account_number", ""))} - {""}
    if expected not in known:
        return GateResult("account_identity", False,
                          f"{profile_name} credentials resolved to an unexpected account")
    if profile_name == "competition" and not in_scored_window(now):
        return GateResult("account_identity", False,
                          "competition account addressed outside the scored window")
    where = "in window" if profile_name == "competition" else "verified"
    return GateResult("account_identity", True, f"{profile_name} account, {where}")


def g_account_tradable(account: dict) -> GateResult:
    if account.get("trading_blocked") or account.get("account_blocked"):
        return GateResult("account_tradable", False, "account or trading is blocked")
    if int(account.get("options_trading_level") or 0) < 3:
        return GateResult("account_tradable", False,
                          f"options level {account.get('options_trading_level')} < 3")
    return GateResult("account_tradable", True, "active, options level 3")


def g_starting_equity(account: dict, expected: float = 100_000.0) -> GateResult:
    equity = float(account.get("equity", 0))
    ok = abs(equity - expected) <= 0.01
    return GateResult("starting_equity", ok, f"equity ${equity:,.2f}")


# ---------------------------------------------------------------- quote validity

def g_quote_valid(symbol: str, quote: dict, params: RiskParams,
                  now: dt.datetime | None = None) -> GateResult:
    """Present, uncrossed, non-zero bid, fresh. A zero bid means no exit exists."""
    if not quote:
        return GateResult("quote_valid", False, f"{symbol}: no quote")
    bid, ask = float(quote.get("bp", 0) or 0), float(quote.get("ap", 0) or 0)
    if bid <= 0:
        return GateResult("quote_valid", False,
                          f"{symbol}: zero bid -- buyable, not sellable, no exit at any price")
    if ask <= 0:
        return GateResult("quote_valid", False, f"{symbol}: no ask")
    if ask < bid:
        return GateResult("quote_valid", False, f"{symbol}: crossed {bid:.2f}/{ask:.2f}")
    ts = quote.get("t")
    if ts:
        when = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age = ((now or dt.datetime.now(dt.timezone.utc)) - when).total_seconds()
        if age > params.max_quote_age_s:
            return GateResult("quote_valid", False, f"{symbol}: quote {age:.0f}s stale")
    return GateResult("quote_valid", True, f"{symbol}: {bid:.2f}/{ask:.2f}")


def g_spread(symbol: str, quote: dict, params: RiskParams) -> GateResult:
    """Percentage of mid, with an absolute allowance.

    Percentage alone is unstable on cheap contracts: a five-cent spread is 50% of a
    ten-cent option and 1% of a five-dollar one. Measured at-the-money spreads run
    2-6% while the same contracts sit at three to fifteen cents, so either test
    passing is enough.
    """
    bid, ask = float(quote.get("bp", 0) or 0), float(quote.get("ap", 0) or 0)
    mid = (bid + ask) / 2
    if mid <= 0:
        return GateResult("spread", False, f"{symbol}: no mid")
    absolute = ask - bid
    pct = absolute / mid * 100
    by_pct = pct <= params.max_spread_pct_of_mid
    # The allowance rescues a tick-sized spread on a fairly priced contract. It must
    # not rescue a near-worthless one: $0.02/$0.07 is only a five-cent spread and
    # still costs 71% of the ask to cross.
    by_abs = absolute <= params.max_spread_abs and pct <= params.spread_pct_ceiling
    ok = by_pct or by_abs
    how = "pct" if by_pct else ("abs" if by_abs else "neither")
    return GateResult("spread", ok,
                      f"{symbol}: {pct:.1f}% / ${absolute:.2f} "
                      f"(limits {params.max_spread_pct_of_mid:.1f}% or "
                      f"${params.max_spread_abs:.2f} under "
                      f"{params.spread_pct_ceiling:.0f}%; passed on {how})")


# ---------------------------------------------------------------- economics

def g_economics(legs, net_price: float, qty: int, params: RiskParams) -> GateResult:
    """Max profit > 0, max loss > 0, risk/reward above the floor."""
    ml = st.max_loss(legs, net_price, qty)
    mp = st.max_profit(legs, net_price, qty)
    if mp != st.UNBOUNDED and mp <= 0:
        width = st.strike_width(legs) * qty
        return GateResult("economics", False,
                          f"max profit ${mp:,.0f} <= 0 -- net debit at or above the "
                          f"${width:,.0f} width, a loss at every outcome")
    if ml <= 0:
        return GateResult("economics", False,
                          f"max loss ${ml:,.0f} <= 0 -- implausible risk-free result, refusing")
    if mp != st.UNBOUNDED:
        rr = mp / ml
        if rr < params.min_risk_reward:
            return GateResult("economics", False,
                              f"risk/reward {rr:.2f} < {params.min_risk_reward:.2f} "
                              f"(profit ${mp:,.0f}, loss ${ml:,.0f})")
        return GateResult("economics", True,
                          f"profit ${mp:,.0f}, loss ${ml:,.0f}, r/r {rr:.2f}")
    return GateResult("economics", True, f"loss ${ml:,.0f}, profit unbounded")


def g_structure(legs, underlying: str | None = None) -> GateResult:
    """One bounded structure with valid ratios, intents, and expiration."""
    if not legs:
        return GateResult("structure", False, "no legs")
    if len(legs) > 4:
        return GateResult("structure", False, f"{len(legs)} legs exceeds the 4-leg maximum")
    roots = {l.symbol[:l.symbol.find("2")] if "2" in l.symbol else l.symbol for l in legs}
    if len(roots) > 1:
        return GateResult("structure", False, f"legs span several underlyings: {roots}")
    if underlying and roots != {underlying}:
        return GateResult("structure", False,
                          f"declared underlying {underlying} does not match legs {roots}")
    expiries = {l.expiry for l in legs}
    if len(expiries) > 1:
        return GateResult("structure", False,
                          "multi-expiration structures are outside the bounded-risk runtime")
    for l in legs:
        if not isinstance(l.ratio_qty, int) or l.ratio_qty <= 0:
            return GateResult("structure", False,
                              f"{l.symbol}: ratio_qty must be a positive integer")
        expected = {("buy", "buy_to_open"), ("sell", "sell_to_open"),
                    ("buy", "buy_to_close"), ("sell", "sell_to_close")}
        if (l.side, l.position_intent) not in expected:
            return GateResult("structure", False,
                              f"{l.symbol}: side {l.side} contradicts intent {l.position_intent}")
        if not l.position_intent.endswith("_open"):
            return GateResult("structure", False,
                              f"{l.symbol}: generated trade intents may only open exposure")
    if math.gcd(*(l.ratio_qty for l in legs)) != 1:
        return GateResult("structure", False, "leg ratios must be reduced to lowest terms")
    if st.has_unbounded_loss(legs):
        return GateResult("structure", False, "net short-call exposure has unbounded loss")
    return GateResult("structure", True, f"{len(legs)} legs, one underlying, intents consistent")


# ---------------------------------------------------------------- portfolio

def g_risk_budget(max_loss_dollars: float, equity: float, open_premium_at_risk: float,
                  realised_loss: float, params: RiskParams) -> GateResult:
    single_cap = equity * params.max_single_position_pct / 100
    if max_loss_dollars > single_cap:
        return GateResult("risk_budget", False,
                          f"${max_loss_dollars:,.0f} exceeds single-position cap ${single_cap:,.0f}")
    total_cap = equity * params.max_total_premium_at_risk_pct / 100
    if open_premium_at_risk + max_loss_dollars > total_cap:
        return GateResult("risk_budget", False,
                          f"would put ${open_premium_at_risk + max_loss_dollars:,.0f} at risk "
                          f"against a ${total_cap:,.0f} cap")
    throttle = equity * params.realised_loss_throttle_pct / 100
    if realised_loss >= throttle:
        return GateResult("risk_budget", False,
                          f"realised losses ${realised_loss:,.0f} past the ${throttle:,.0f} "
                          f"throttle -- no new entries, open positions untouched")
    return GateResult("risk_budget", True,
                      f"${max_loss_dollars:,.0f} within caps "
                      f"(single ${single_cap:,.0f}, total ${total_cap:,.0f})")


INDEX_CLUSTER = frozenset({"SPY", "QQQ", "IWM"})
SHORT_GAMMA_FAMILIES = frozenset({
    "iron_condor", "iron_butterfly", "vertical_call", "vertical_put",
})


def _short_gamma_position(position: dict) -> bool:
    if str(position.get("family") or "") not in SHORT_GAMMA_FAMILIES:
        return False
    premium_type = position.get("premium_type")
    if premium_type is not None:
        return str(premium_type) == "short"
    entry = position.get("entry_price_per_unit")
    if entry is not None:
        return float(entry) < 0
    return float(position.get("entry_notional") or 0) < 0


def g_concentration(underlying: str, open_positions: list, params: RiskParams,
                    *, family: str | None = None,
                    net_price: float | None = None,
                    risk_reducing: bool = False) -> GateResult:
    n_total = len(open_positions)
    n_under = sum(1 for p in open_positions if p.get("underlying") == underlying)
    if risk_reducing:
        return GateResult(
            "concentration", True,
            "candidate has a positive measured contribution in the current binding "
            "scenario and the resulting book remains inside the scenario limit")
    if n_total >= params.max_concurrent_positions:
        return GateResult("concentration", False,
                          f"{n_total} open positions at the {params.max_concurrent_positions} cap")
    if n_under >= params.max_positions_per_underlying:
        return GateResult("concentration", False,
                          f"{n_under} positions already in {underlying} "
                          f"(cap {params.max_positions_per_underlying})")
    candidate_short_gamma = (
        str(underlying).upper() in INDEX_CLUSTER
        and str(family or "") in SHORT_GAMMA_FAMILIES
        and net_price is not None and float(net_price) < 0)
    cluster_short_gamma = sum(
        1 for position in open_positions
        if str(position.get("underlying") or "").upper() in INDEX_CLUSTER
        and _short_gamma_position(position))
    if (candidate_short_gamma
            and cluster_short_gamma
            >= params.max_correlated_index_short_gamma_positions):
        return GateResult(
            "concentration", False,
            f"{cluster_short_gamma} correlated SPY/QQQ/IWM short-gamma structures "
            f"already open (cap {params.max_correlated_index_short_gamma_positions})")
    return GateResult("concentration", True, f"{n_total} open, {n_under} in {underlying}")


def g_buying_power(max_loss_dollars: float, account: dict) -> GateResult:
    obp = float(account.get("options_buying_power", account.get("buying_power", 0)) or 0)
    ok = max_loss_dollars <= obp
    return GateResult("buying_power", ok,
                      f"needs ${max_loss_dollars:,.0f}, options buying power ${obp:,.0f}")


def render(results: list[GateResult]) -> str:
    lines = [r.render() for r in results]
    verdict = "EXECUTABLE" if all(r.passed for r in results) else "BLOCKED"
    return "\n".join(lines) + f"\n\n{verdict}"


def all_passed(results: list[GateResult]) -> bool:
    return all(r.passed for r in results)
