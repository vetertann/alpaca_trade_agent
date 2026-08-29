"""The agent loop.

Monitoring is continuous and cheap; deciding is rare and expensive. The tiers keep
those apart. Tier 0 and Tier 1 never touch a model.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from agent.config import ET, WINDOW_CLOSE, in_scored_window

WARM_UP_MINUTES = 15          # spreads are widest into the opening auction
WIND_DOWN_ET = dt.time(15, 45)
FINAL_SESSION_WIND_DOWN_ET = dt.time(15, 0)
DEBOUNCE_SECONDS = 600
MAX_CYCLES_PER_SESSION = 20
DEPLOYMENT_FLOOR_ET = dt.time(10, 30)

ANCHORS = (dt.time(9, 45), dt.time(11, 0), dt.time(14, 0), dt.time(15, 30))


def session_state(now_et: dt.datetime, trading_day: bool = True) -> str:
    """Time of day alone is not enough -- a weekend morning is not WARM_UP."""
    if not trading_day or now_et.weekday() >= 5:
        return "CLOSED"
    t = now_et.time()
    if t < dt.time(9, 30) or t >= dt.time(16, 0):
        return "CLOSED"
    if t < dt.time(9, 45):
        return "WARM_UP"
    wind = (FINAL_SESSION_WIND_DOWN_ET if now_et.date() == WINDOW_CLOSE.date()
            else WIND_DOWN_ET)
    if t >= wind:
        return "WINDING_DOWN"
    return "ACTIVE"


def entries_allowed(now_et: dt.datetime, trading_day: bool = True) -> tuple[bool, str]:
    if not in_scored_window(now_et):
        return False, "outside the scored window"
    state = session_state(now_et, trading_day)
    if state != "ACTIVE":
        return False, f"session state {state}"
    return True, "active"


@dataclass
class Trigger:
    name: str
    detail: str
    value: float | None = None
    exempt_from_debounce: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "detail": self.detail, "value": self.value}


@dataclass
class TriggerState:
    """Tier 1. Numeric predicates over state Tier 0 maintains. No model involved."""
    last_cycle_at: dt.datetime | None = None
    last_anchor_fired: dt.time | None = None
    cycles_this_session: int = 0
    baseline: dict = field(default_factory=dict)     # per-symbol snapshot at last cycle
    deployment_floor_fired: bool = False

    def evaluate(self, now_et: dt.datetime, universe: dict, book: list,
                 expected_daily_move: dict | None = None,
                 trading_day: bool = True) -> Trigger | None:
        if session_state(now_et, trading_day) == "CLOSED":
            return None
        expected_daily_move = expected_daily_move or {}

        # --- exempt triggers first: these bypass debounce -------------------
        if (not self.deployment_floor_fired and not book
                and now_et.time() >= DEPLOYMENT_FLOOR_ET
                and now_et.date() == WINDOW_CLOSE.date() - dt.timedelta(days=3)):
            return Trigger("deployment_floor",
                           "no position open by 10:30 ET on the first session",
                           exempt_from_debounce=True)

        # --- scheduled anchors ----------------------------------------------
        for anchor in ANCHORS:
            if now_et.time() >= anchor and (self.last_anchor_fired is None
                                            or self.last_anchor_fired < anchor):
                return Trigger("session_anchor", f"{anchor:%H:%M} ET anchor")

        if self._debounced(now_et):
            return None
        if self.cycles_this_session >= MAX_CYCLES_PER_SESSION:
            return None

        # --- market-state predicates ----------------------------------------
        for sym, cur in universe.items():
            base = self.baseline.get(sym)
            if not base:
                continue
            spot, old = cur.get("spot"), base.get("spot")
            if spot and old:
                move = abs(spot / old - 1)
                em = expected_daily_move.get(sym)
                if em and move > 0.5 * em:
                    return Trigger("underlying_move",
                                   f"{sym} moved {move*100:.2f}% since the last cycle, "
                                   f"past half the {em*100:.2f}% expected daily move",
                                   round(move, 5))
            ratio, old_ratio = cur.get("iv_rv_ratio"), base.get("iv_rv_ratio")
            if ratio and old_ratio and old_ratio > 0:
                rel = abs(ratio / old_ratio - 1)
                if rel > 0.10:
                    return Trigger("volatility_shift",
                                   f"{sym} iv/rv moved {rel*100:.1f}% since the last cycle",
                                   round(rel, 4))
        return None

    def _debounced(self, now_et: dt.datetime) -> bool:
        if self.last_cycle_at is None:
            return False
        return (now_et - self.last_cycle_at).total_seconds() < DEBOUNCE_SECONDS

    def record_cycle(self, now_et: dt.datetime, universe: dict,
                     trigger: Trigger | None = None) -> None:
        self.last_cycle_at = now_et
        self.cycles_this_session += 1
        self.baseline = {s: dict(v) for s, v in universe.items()}
        # Any cycle satisfies every anchor already passed -- otherwise a cycle
        # driven by a market trigger leaves stale anchors that fire immediately
        # afterwards and mask the next real predicate.
        for anchor in ANCHORS:
            if now_et.time() >= anchor:
                self.last_anchor_fired = anchor
        if trigger and trigger.name == "deployment_floor":
            self.deployment_floor_fired = True

    def new_session(self) -> None:
        self.cycles_this_session = 0
        self.last_anchor_fired = None
        self.last_cycle_at = None


def position_exit_due(position: dict, thesis: dict, now_et: dt.datetime,
                      params) -> tuple[bool, str]:
    """Tier 0 exit evaluation. Deterministic, no model.

    Long premium carries no drawdown stop: maximum loss is the premium and is
    bounded at entry, so a stop there sells the convexity it was bought for.
    """
    unreal = float(position.get("unrealized_pl") or 0)
    basis = abs(float(position.get("cost_basis") or 0))
    is_long_premium = float(position.get("cost_basis") or 0) > 0

    if basis > 0 and unreal >= basis * params.profit_target_pct / 100:
        return True, f"profit target: +${unreal:,.0f} on ${basis:,.0f} basis"

    if not is_long_premium:
        credit = basis or 1.0
        if unreal <= -credit * params.short_premium_stop_multiple:
            return True, (f"short-premium stop: -${abs(unreal):,.0f} past "
                          f"{params.short_premium_stop_multiple}x credit")

    if session_state(now_et) == "WINDING_DOWN" and now_et.date() == WINDOW_CLOSE.date():
        return True, "time stop: final session winding down"
    return False, ""
