"""Host-owned decision-horizon option valuation.

The account is judged on total marked equity at the Thursday close.  An option
expiring after that instant still owns time value, so expiry payoff is not a
valid proxy for its score contribution.  The explicitly authorized Friday paper
session uses Friday close instead.  These helpers keep both timestamps and the
executable mark convention host-owned without rewriting the official score.
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping

from agent.config import AUTONOMOUS_TRADING_END, ET, MEASUREMENT_END, WINDOW_CLOSE
from agent.quant import bs
from agent.types import CONTRACT_MULTIPLIER

SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)
SESSION_SECONDS = 6.5 * 60 * 60


def expiry_close(expiry: str | dt.date | dt.datetime) -> dt.datetime:
    if isinstance(expiry, dt.datetime):
        value = expiry
        if value.tzinfo is None:
            value = value.replace(tzinfo=ET)
        return value.astimezone(ET)
    value = expiry if isinstance(expiry, dt.date) else dt.date.fromisoformat(str(expiry))
    return dt.datetime.combine(value, SESSION_CLOSE, tzinfo=ET)


def decision_horizon(now: dt.datetime | None = None) -> dt.datetime:
    """Active economic horizon without rewriting the official score timestamp."""
    if now is None:
        return WINDOW_CLOSE
    observed = now if now.tzinfo is not None else now.replace(tzinfo=ET)
    return (AUTONOMOUS_TRADING_END
            if observed.astimezone(ET) >= MEASUREMENT_END else WINDOW_CLOSE)


def evaluation_at(expiry: str | dt.date | dt.datetime,
                  now: dt.datetime | None = None) -> dt.datetime:
    """Earlier of contract expiry and the active, host-owned decision horizon."""
    return min(expiry_close(expiry), decision_horizon(now))


def trading_days_between(now: dt.datetime, target: dt.datetime) -> float:
    """Fractional regular trading sessions between two aware timestamps."""
    if now.tzinfo is None or target.tzinfo is None:
        raise ValueError("score-horizon timestamps must be timezone-aware")
    start, end = now.astimezone(ET), target.astimezone(ET)
    if end <= start:
        return 0.0
    total = 0.0
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5:
            opened = dt.datetime.combine(day, SESSION_OPEN, tzinfo=ET)
            closed = dt.datetime.combine(day, SESSION_CLOSE, tzinfo=ET)
            left, right = max(start, opened), min(end, closed)
            if right > left:
                total += (right - left).total_seconds() / SESSION_SECONDS
        day += dt.timedelta(days=1)
    return total


def candidate_horizon(expiry: str | dt.date | dt.datetime,
                      now: dt.datetime) -> dict:
    horizon_end = decision_horizon(now)
    at = evaluation_at(expiry, now)
    contract_expiry = expiry_close(expiry)
    trading_days = max(trading_days_between(now, at), 1.0 / 390.0)
    residual = max((contract_expiry - at).total_seconds() / 86400.0, 0.0)
    return {
        "evaluation_at": at.isoformat(timespec="seconds"),
        "score_horizon_trading_days": round(trading_days, 6),
        "residual_calendar_days_at_evaluation": round(residual, 6),
        "valuation_basis": (
            "expiry_payoff" if residual <= 0 else
            ("Thursday score-time executable mark with residual time value"
             if horizon_end == WINDOW_CLOSE else
             "Friday post-submission executable mark with residual time value")),
        "horizon_kind": ("official_score" if horizon_end == WINDOW_CLOSE
                         else "post_submission_paper_session"),
    }


def executable_value(candidate, spot: float, at: dt.datetime, *,
                     iv_multiplier: float = 1.0) -> float:
    """Signed closeable structure value per unit in dollars at ``at``.

    Per-leg midpoint IV and half-spread are captured when the candidate is
    enumerated.  The same observed half-spread is retained at the horizon so a
    theoretical midpoint move cannot masquerade as realizable account value.
    """
    inputs: Mapping = candidate.detail.get("leg_valuation_inputs") or {}
    total = 0.0
    for leg in candidate.legs:
        row = inputs.get(leg.symbol) or {}
        iv = float(row.get("iv") or 0)
        half_spread = float(row.get("half_spread") or 0)
        if iv <= 0 or not math.isfinite(iv):
            raise ValueError(f"{leg.symbol}: no usable IV for score-horizon valuation")
        expiry = expiry_close(leg.expiry)
        if at.astimezone(dt.timezone.utc) >= expiry.astimezone(dt.timezone.utc):
            theoretical = (max(float(spot) - leg.strike, 0.0)
                           if leg.option_type == "call"
                           else max(leg.strike - float(spot), 0.0))
        else:
            t = bs.year_fraction(at.astimezone(dt.timezone.utc),
                                 expiry.astimezone(dt.timezone.utc))
            theoretical = bs.price(
                float(spot), leg.strike, t, iv * float(iv_multiplier), leg.option_type)
        # Unwind an opening long at bid and an opening short at ask.
        executable = (max(theoretical - half_spread, 0.0)
                      if leg.sign > 0 else theoretical + half_spread)
        total += leg.sign * leg.ratio_qty * executable * CONTRACT_MULTIPLIER
    return total
