"""Risk parameters. Starting values; the agent may propose revisions within bounds."""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RiskParams:
    max_total_premium_at_risk_pct: float = 40.0
    max_single_position_pct: float = 15.0
    realised_loss_throttle_pct: float = 12.0
    max_concurrent_positions: int = 8
    max_positions_per_underlying: int = 4
    # Calibrated from the at-the-money band, 2026-08-29 closing quotes -- an upper
    # bound, since closing spreads are wider than intraday. Re-run scripts/calibrate.py
    # after 09:35 ET on Monday before the first entry.
    max_spread_pct_of_mid: float = 8.9
    max_spread_abs: float = 0.22            # allowance so cheap contracts are not
                                            # rejected on percentage arithmetic alone
    spread_pct_ceiling: float = 25.0        # the allowance never rescues this far
    min_risk_reward: float = 0.25
    profit_target_pct: float = 50.0
    short_premium_stop_multiple: float = 2.0
    min_bid: float = 0.01                   # a zero bid means no exit exists
    max_quote_age_s: float = 90.0

    # Bounds the host enforces on any revision the model proposes.
    BOUNDS = {
        "max_total_premium_at_risk_pct": (5.0, 60.0),
        "max_single_position_pct": (1.0, 25.0),
        "realised_loss_throttle_pct": (5.0, 25.0),
        "max_concurrent_positions": (1, 16),
        "max_positions_per_underlying": (1, 8),
        "max_spread_pct_of_mid": (1.0, 25.0),
        "max_spread_abs": (0.01, 2.0),
        "spread_pct_ceiling": (10.0, 60.0),
        "min_risk_reward": (0.10, 2.0),
        "profit_target_pct": (20.0, 90.0),
        "short_premium_stop_multiple": (1.0, 4.0),
        "max_quote_age_s": (5.0, 600.0),
    }

    def revise(self, **changes) -> "RiskParams":
        """Apply a proposed revision, clamped to bounds. Unknown keys are refused."""
        clean = {}
        for k, v in changes.items():
            if k not in self.BOUNDS:
                raise ValueError(f"{k} is not a revisable risk parameter")
            lo, hi = self.BOUNDS[k]
            clamped = type(getattr(self, k))(min(max(v, lo), hi))
            if clamped != v:
                print(f"[risk] {k}={v} clamped to {clamped} (bounds {lo}-{hi})")
            clean[k] = clamped
        return replace(self, **clean)


DEFAULT = RiskParams()
