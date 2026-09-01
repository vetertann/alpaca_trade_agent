"""The agent loop.

Monitoring is continuous and cheap; deciding is rare and expensive. The tiers keep
those apart. Tier 0 and Tier 1 never touch a model.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from agent.config import ET, WINDOW_CLOSE, in_scored_window

WARM_UP_MINUTES = 15          # spreads are widest into the opening auction
WIND_DOWN_ET = dt.time(15, 45)
EXPIRY_LIQUIDATION_ET = dt.time(15, 15)
FINAL_SESSION_WIND_DOWN_ET = dt.time(15, 0)
DEBOUNCE_SECONDS = 300
FLAT_REVIEW_SECONDS = 1200
# Eight is the operational hard capacity, not a portfolio target.  Routine build
# reviews continue until the scenario-risk budget is substantially used; the
# model is still free to decline every candidate.
INITIAL_ALLOCATION_CAPACITY = 8
INITIAL_ALLOCATION_TARGET_RISK_PCT = 0.035
MAX_CYCLES_PER_SESSION = 24
DEPLOYMENT_FLOOR_ET = dt.time(10, 30)
SHORT_PREMIUM_MAX_LOSS_STOP_FRACTION = 0.50
PORTFOLIO_EQUITY_REVIEW_PCT = 0.0015
STRUCTURE_PNL_REVIEW_PCT = 0.001
STOP_REVIEW_PROGRESS = 0.50

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
    portfolio_baseline: dict = field(default_factory=dict)
    deployment_floor_fired: bool = False
    scenario_breach_latched: bool = False

    def to_json(self) -> dict:
        return {
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "last_anchor_fired": (self.last_anchor_fired.isoformat()
                                  if self.last_anchor_fired else None),
            "cycles_this_session": self.cycles_this_session,
            "baseline": self.baseline,
            "portfolio_baseline": self.portfolio_baseline,
            "deployment_floor_fired": self.deployment_floor_fired,
            "scenario_breach_latched": self.scenario_breach_latched,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "TriggerState":
        return cls(
            last_cycle_at=(dt.datetime.fromisoformat(raw["last_cycle_at"])
                           if raw.get("last_cycle_at") else None),
            last_anchor_fired=(dt.time.fromisoformat(raw["last_anchor_fired"])
                               if raw.get("last_anchor_fired") else None),
            cycles_this_session=int(raw.get("cycles_this_session") or 0),
            baseline=dict(raw.get("baseline") or {}),
            portfolio_baseline=dict(raw.get("portfolio_baseline") or {}),
            deployment_floor_fired=bool(raw.get("deployment_floor_fired")),
            scenario_breach_latched=bool(raw.get("scenario_breach_latched")),
        )

    def evaluate(self, now_et: dt.datetime, universe: dict, book: list,
                 expected_daily_move: dict | None = None,
                 structure_count: int | None = None,
                 portfolio_risk_pct: float = 0.0,
                 portfolio_snapshot: dict | None = None,
                 trading_day: bool = True) -> Trigger | None:
        if session_state(now_et, trading_day) == "CLOSED":
            return None
        expected_daily_move = expected_daily_move or {}
        cycle_budget_available = (
            self.cycles_this_session < MAX_CYCLES_PER_SESSION)

        # Resulting-book risk is already outside its calibrated envelope. The
        # first crossing bypasses the ordinary event debounce; the latch clears
        # only below the host-provided hysteresis floor. Exits remain Tier 0 and
        # entry materialisation permits only a complete risk-reducing solution.
        scenario = (portfolio_snapshot or {}).get("portfolio_scenario_risk") or {}
        if scenario.get("status") == "ok":
            loss = float(scenario.get("loss_dollars") or 0)
            clear_below = float(scenario.get("clear_below_dollars") or 0)
            breached = bool(scenario.get("breached"))
            if self.scenario_breach_latched and loss <= clear_below:
                self.scenario_breach_latched = False
            if breached and not self.scenario_breach_latched:
                self.scenario_breach_latched = True
                if cycle_budget_available:
                    return Trigger(
                        "portfolio_scenario_breach",
                        f"correlated scenario loss ${loss:,.0f} exceeds the "
                        f"${float(scenario.get('limit_dollars') or 0):,.0f} limit; "
                        "new risk is restricted to mathematically repairing structures",
                        round(loss, 2), exempt_from_debounce=True)

        # --- exempt triggers first: these bypass debounce -------------------
        if (not self.deployment_floor_fired and not book
                and now_et.time() >= DEPLOYMENT_FLOOR_ET
                and now_et.date() == WINDOW_CLOSE.date() - dt.timedelta(days=3)
                and cycle_budget_available):
            return Trigger("deployment_floor",
                           "no position open by 10:30 ET on the first session",
                           exempt_from_debounce=True)

        # A position approaching its deterministic stop deserves a fresh model
        # review even inside the ordinary event debounce. The actual stop remains
        # Tier 0 and does not depend on the model running successfully.
        current_structures = {
            str(row.get("structure_id")): row
            for row in (portfolio_snapshot or {}).get("structures") or []
            if row.get("structure_id")
        }
        baseline_structures = self.portfolio_baseline.get("structures") or {}
        for sid, cur in current_structures.items():
            progress = cur.get("stop_progress")
            old_progress = (baseline_structures.get(sid) or {}).get("stop_progress")
            if (progress is not None and float(progress) >= STOP_REVIEW_PROGRESS
                    and (old_progress is None
                         or float(old_progress) < STOP_REVIEW_PROGRESS)
                    and cycle_budget_available):
                return Trigger(
                    "stop_approach",
                    f"{sid} reached {float(progress):.0%} of its deterministic loss stop",
                    round(float(progress), 4), exempt_from_debounce=True)

        # --- scheduled anchors ----------------------------------------------
        for anchor in ANCHORS:
            if now_et.time() >= anchor and (self.last_anchor_fired is None
                                            or self.last_anchor_fired < anchor) \
                    and cycle_budget_available:
                return Trigger("session_anchor", f"{anchor:%H:%M} ET anchor")

        if self._debounced(now_et):
            return None
        if not cycle_budget_available:
            return None

        # Portfolio predicates compare continuously sampled marks with the last
        # decision baseline. They are independent of the slower preflight bundle.
        if portfolio_snapshot and self.portfolio_baseline:
            equity = float(portfolio_snapshot.get("equity") or 0)
            old_equity = float(self.portfolio_baseline.get("equity") or 0)
            equity_drop = old_equity - equity
            equity_floor = max(abs(old_equity) * PORTFOLIO_EQUITY_REVIEW_PCT, 100.0)
            if old_equity and equity_drop >= equity_floor:
                return Trigger(
                    "portfolio_deterioration",
                    f"equity fell ${equity_drop:,.0f} since the last decision cycle",
                    round(equity_drop, 2))
            structure_floor = max(abs(old_equity) * STRUCTURE_PNL_REVIEW_PCT, 75.0)
            for sid, cur in current_structures.items():
                old = baseline_structures.get(sid)
                if not old:
                    continue
                cur_pnl = float(cur.get("broker_unrealized_pl", cur.get("unrealized_pl"))
                                or 0)
                old_pnl = float(old.get("unrealized_pl") or 0)
                deterioration = old_pnl - cur_pnl
                if deterioration >= structure_floor:
                    return Trigger(
                        "structure_deterioration",
                        f"{sid} unrealized P&L deteriorated ${deterioration:,.0f} "
                        "since the last decision cycle",
                        round(deterioration, 2))
                old_expiry_pnl = old.get("pnl_if_expired_now_per_unit")
                cur_expiry_pnl = cur.get("pnl_if_expired_now_per_unit")
                if (old_expiry_pnl is not None and cur_expiry_pnl is not None
                        and float(old_expiry_pnl) >= 0 > float(cur_expiry_pnl)):
                    return Trigger(
                        "breakeven_cross",
                        f"{sid} crossed from profitable to unprofitable at expiry "
                        "at the current underlying price",
                        round(float(cur_expiry_pnl), 2))

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

        # Build sequentially: one verified structure per cycle, revisiting until
        # the scenario-risk target is used or the operational hard capacity is hit.
        # Position count is not itself an allocation objective.
        # Market-change predicates take priority so the cycle gets the specific cause.
        count = len(book) if structure_count is None else structure_count
        allocation_needed = (
            count < INITIAL_ALLOCATION_CAPACITY
            and portfolio_risk_pct < INITIAL_ALLOCATION_TARGET_RISK_PCT)
        if (allocation_needed and self.last_cycle_at is not None
                and (now_et - self.last_cycle_at).total_seconds() >= FLAT_REVIEW_SECONDS):
            return Trigger(
                "portfolio_build_review",
                f"portfolio scenario risk remains below "
                f"{INITIAL_ALLOCATION_TARGET_RISK_PCT:.1%} with capacity available "
                "and has not been reviewed for 20 minutes")
        return None

    def _debounced(self, now_et: dt.datetime) -> bool:
        if self.last_cycle_at is None:
            return False
        return (now_et - self.last_cycle_at).total_seconds() < DEBOUNCE_SECONDS

    def record_cycle(self, now_et: dt.datetime, universe: dict,
                     trigger: Trigger | None = None,
                     portfolio_snapshot: dict | None = None) -> None:
        self.last_cycle_at = now_et
        self.cycles_this_session += 1
        self.baseline = {s: dict(v) for s, v in universe.items()}
        if portfolio_snapshot is not None:
            self.portfolio_baseline = {
                "equity": portfolio_snapshot.get("equity"),
                "structures": {
                    str(row.get("structure_id")): {
                        "unrealized_pl": row.get(
                            "broker_unrealized_pl", row.get("unrealized_pl")),
                        "stop_progress": row.get("stop_progress"),
                        "pnl_if_expired_now_per_unit": row.get(
                            "pnl_if_expired_now_per_unit"),
                    }
                    for row in portfolio_snapshot.get("structures") or []
                    if row.get("structure_id")
                },
            }
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
        self.baseline = {}
        self.portfolio_baseline = {}
        self.deployment_floor_fired = False
        self.scenario_breach_latched = False


def _thesis_value(thesis, name: str):
    if isinstance(thesis, dict):
        return thesis.get(name)
    return getattr(thesis, name, None) if thesis is not None else None


def _deadline(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw.upper().endswith(" ET"):
        raw = raw[:-3].strip()
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _expiry_deadline(position: dict) -> dt.datetime | None:
    expiries = []
    for leg in position.get("legs") or []:
        try:
            expiries.append(dt.date.fromisoformat(str(leg.get("expiry"))))
        except (TypeError, ValueError):
            continue
    if not expiries:
        return None
    # Never let a structure drift into exercise/assignment merely because a
    # generated thesis omitted or misspelled its prose deadline.
    return dt.datetime.combine(min(expiries), EXPIRY_LIQUIDATION_ET, tzinfo=ET)


def position_exit_due(position: dict, thesis, now_et: dt.datetime,
                      params, *, require_executable_profit: bool = False,
                      settlement_authorized: bool = False) -> tuple[bool, str]:
    """Tier 0 exit evaluation. Deterministic, no model.

    Long premium carries no drawdown stop: maximum loss is the premium and is
    bounded at entry, so a stop there sells the convexity it was bought for.
    """
    broker_unreal = float(position.get(
        "broker_unrealized_pl", position.get("unrealized_pl")) or 0)
    executable_raw = position.get("executable_unrealized_pl")
    executable_unreal = (float(executable_raw)
                         if executable_raw is not None else None)
    # Profit must be fillable. For loss protection use the worse available mark;
    # a wide closing market must never make the stop look safer than it is.
    profit_unreal = executable_unreal if executable_unreal is not None else (
        None if require_executable_profit else broker_unreal)
    loss_unreal = min(broker_unreal, executable_unreal) \
        if executable_unreal is not None else broker_unreal
    basis = abs(float(position.get("cost_basis") or 0))
    is_long_premium = float(position.get("cost_basis") or 0) > 0

    target = float(position.get("profit_target") or 0)
    target_policy_invalid = (
        (position.get("profit_target_policy") or {}).get("validation_status") == "invalid")
    if target <= 0 and basis > 0 and not target_policy_invalid:
        target = basis * params.profit_target_pct / 100
    if target > 0 and profit_unreal is not None and profit_unreal >= target:
        return True, (f"executable profit target: +${profit_unreal:,.0f} "
                      f"against ${target:,.0f} enforced target")

    if not is_long_premium:
        credit = basis or 1.0
        # "2x credit" convention refers to the debit required to close reaching
        # twice the entry credit: a P&L loss of one credit, not two. Capped spreads
        # can make even that price unreachable, so also stop at half of defined
        # maximum loss and take whichever threshold arrives first.
        credit_loss = credit * max(params.short_premium_stop_multiple - 1.0, 0.0)
        max_loss = float(position.get("premium_at_risk") or 0)
        if not max_loss:
            max_loss = (float(position.get("max_loss_per_unit") or 0)
                        * float(position.get("qty") or 0))
        thresholds = [x for x in (
            credit_loss,
            max_loss * SHORT_PREMIUM_MAX_LOSS_STOP_FRACTION
            if math.isfinite(max_loss) else 0,
        ) if x > 0]
        stop_loss = min(thresholds) if thresholds else 0.0
        if stop_loss and loss_unreal <= -stop_loss:
            return True, (f"short-premium stop: -${abs(loss_unreal):,.0f} past "
                          f"${stop_loss:,.0f} loss threshold")

    explicit = (_deadline(_thesis_value(thesis, "exit_at"))
                or _deadline(_thesis_value(thesis, "exit_time")))
    if explicit is not None and now_et >= explicit:
        return True, f"thesis time stop: {explicit:%Y-%m-%d %H:%M ET}"

    expiry = _expiry_deadline(position)
    if expiry is not None and now_et >= expiry and not settlement_authorized:
        return True, (f"expiry-day mandatory liquidation: "
                      f"{expiry:%Y-%m-%d %H:%M ET}; no currently valid settlement authorization")

    if session_state(now_et) == "WINDING_DOWN" and now_et.date() == WINDOW_CLOSE.date():
        # Total equity is scored, not realised cash.  A later-dated option keeps
        # contributing its marked value after Thursday's close, so crossing the
        # exit spread merely to turn that mark into cash is not automatically
        # beneficial.  Expiring/unknown structures still flatten defensively;
        # later contracts remain subject to their thesis, profit and risk stops.
        if expiry is None or (expiry.date() <= WINDOW_CLOSE.date()
                              and not settlement_authorized):
            return True, "final-session time stop: expiring book winding down"
    return False, ""
