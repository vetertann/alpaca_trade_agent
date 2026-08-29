"""Structure economics: payoff, maximum loss, maximum profit.

`max_loss` implements the method Alpaca uses for maintenance margin -- the
"universal spread rule": compute intrinsic value at every strike present in the
structure, net the payoffs at each point, take the worst, evaluate per expiration
and take the largest requirement across expirations. Matching their method keeps
our risk model aligned with actual buying power.
"""
from __future__ import annotations

import math
from collections import defaultdict

from agent.types import CONTRACT_MULTIPLIER, Leg

UNBOUNDED = float("inf")


def _leg_intrinsic(leg: Leg, spot: float) -> float:
    if leg.option_type == "call":
        return max(spot - leg.strike, 0.0)
    return max(leg.strike - spot, 0.0)


def net_payoff_at(legs: tuple[Leg, ...] | list[Leg], spot: float) -> float:
    """Per-unit payoff of the structure at expiry, in dollars, premium excluded."""
    return sum(leg.sign * leg.ratio_qty * _leg_intrinsic(leg, spot)
               for leg in legs) * CONTRACT_MULTIPLIER


def _evaluation_points(legs) -> list[float]:
    """Every strike present, plus the boundaries that reveal unbounded risk."""
    strikes = sorted({leg.strike for leg in legs})
    hi = strikes[-1] * 2.0 + 1.0
    return [0.0, *strikes, hi]


def payoff_curve(legs, net_price: float, qty: int = 1, points: int = 0):
    """(spot, pnl) pairs. `net_price` is per unit, debit positive."""
    strikes = sorted({leg.strike for leg in legs})
    lo, hi = strikes[0] * 0.9, strikes[-1] * 1.1
    grid = ([lo + (hi - lo) * i / (points - 1) for i in range(points)] if points
            else _evaluation_points(legs))
    cost = net_price * qty * CONTRACT_MULTIPLIER
    return [(p, net_payoff_at(legs, p) * qty - cost) for p in grid]


def _pnl_extremes(legs, net_price: float, qty: int) -> tuple[float, float]:
    cost = net_price * qty * CONTRACT_MULTIPLIER
    vals = [net_payoff_at(legs, p) * qty - cost for p in _evaluation_points(legs)]
    return min(vals), max(vals)


def has_unbounded_loss(legs) -> bool:
    """Net short call exposure loses without bound as the underlying rises."""
    call_slope = sum(l.sign * l.ratio_qty for l in legs if l.option_type == "call")
    return call_slope < 0


def max_loss(legs, net_price: float, qty: int = 1) -> float:
    """Worst-case loss in dollars, positive. Alpaca's universal spread rule.

    Legs are grouped by expiration; the requirement is the largest across groups.
    """
    if has_unbounded_loss(legs):
        return UNBOUNDED

    by_expiry: dict[object, list[Leg]] = defaultdict(list)
    for leg in legs:
        by_expiry[leg.expiry].append(leg)

    if len(by_expiry) == 1:
        worst, _ = _pnl_extremes(legs, net_price, qty)
        return abs(min(worst, 0.0))

    # Multiple expirations: evaluate each independently, premium attributed once.
    worst_total = 0.0
    for group in by_expiry.values():
        worst, _ = _pnl_extremes(group, net_price, qty)
        worst_total = max(worst_total, abs(min(worst, 0.0)))
    return worst_total


def max_profit(legs, net_price: float, qty: int = 1) -> float:
    """Best case in dollars. Negative means the structure cannot profit at all."""
    _, best = _pnl_extremes(legs, net_price, qty)
    if _is_unbounded_up(legs):
        return UNBOUNDED
    return best


def _is_unbounded_up(legs) -> bool:
    """Net long calls above the highest strike means profit is unbounded."""
    net_calls = sum(l.sign * l.ratio_qty for l in legs if l.option_type == "call")
    return net_calls > 0


def strike_width(legs) -> float:
    strikes = sorted({leg.strike for leg in legs})
    return (strikes[-1] - strikes[0]) * CONTRACT_MULTIPLIER if len(strikes) > 1 else 0.0


def is_net_debit(net_price: float) -> bool:
    return net_price > 0


def is_long_premium(legs, net_price: float) -> bool:
    """Long premium: paid to enter, so maximum loss is bounded by the premium."""
    return net_price > 0
