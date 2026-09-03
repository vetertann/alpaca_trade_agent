"""Host-owned entry-signal policies and deterministic fire-time checks.

The model may choose *how* it wants to enter (continuation, pullback, or a
direction-agnostic volatility entry).  It may not choose the candidate's bias or
rewrite the market evidence: those fields are copied from the exact
``risk.direction`` result recorded by the host.
"""
from __future__ import annotations

import math


MODES = {"momentum_continuation", "pullback_entry", "direction_agnostic"}


def build_policy(entry_evidence: dict | None, *, entry_mode: str = "auto",
                 reference_spot: float | None = None,
                 max_adverse_move_em: float = 0.15,
                 confirmation_samples: int = 2,
                 sample_interval_seconds: float = 1.0) -> dict:
    """Create a canonical policy from candidate-bound host evidence."""
    direction = (entry_evidence or {}).get("direction") or {}
    bias = str(direction.get("candidate_bias") or "unknown")
    directionality = str(direction.get("directionality") or "unknown")
    analysis = direction.get("market_direction") or {}
    analysis_label = str(analysis.get("classification") or "insufficient_data")
    expected_move = direction.get("expected_move")
    spot = reference_spot if reference_spot is not None else direction.get("spot")

    requested = str(entry_mode or "auto").strip().lower()
    if requested == "auto":
        mode = ("direction_agnostic"
                if directionality == "volatility-led" or bias == "neutral"
                else "momentum_continuation")
    elif requested in MODES:
        mode = requested
    else:
        raise ValueError(
            "entry_mode must be auto, momentum_continuation, pullback_entry, "
            "or direction_agnostic")
    if (mode == "direction_agnostic"
            and directionality != "volatility-led" and bias != "neutral"):
        raise ValueError(
            "direction_agnostic is allowed only for host-classified volatility-led "
            "or neutral candidates")

    adverse = float(max_adverse_move_em)
    samples = int(confirmation_samples)
    interval = float(sample_interval_seconds)
    if not math.isfinite(adverse) or not 0.02 <= adverse <= 0.50:
        raise ValueError("max_adverse_move_em must be between 0.02 and 0.50")
    if not 1 <= samples <= 6:
        raise ValueError("signal_confirmation_samples must be between 1 and 6")
    if not math.isfinite(interval) or not 0.5 <= interval <= 10.0:
        raise ValueError("signal_sample_interval_seconds must be between 0.5 and 10")

    if mode != "direction_agnostic":
        if bias not in ("bullish", "bearish"):
            raise ValueError(
                "a directional entry mode requires host-computed bullish or bearish bias")
        try:
            spot_value, move_value = float(spot), float(expected_move)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "a directional entry mode requires host-computed spot and expected_move") \
                from exc
        if (not math.isfinite(spot_value) or spot_value <= 0
                or not math.isfinite(move_value) or move_value <= 0):
            raise ValueError(
                "a directional entry mode requires positive finite spot and expected_move")
    else:
        spot_value = float(spot) if spot is not None else None
        move_value = float(expected_move) if expected_move is not None else None

    return {
        "schema_version": 1,
        "mode": mode,
        "requested_mode": requested,
        "candidate_bias": bias,
        "directionality": directionality,
        "analysis_classification": analysis_label,
        "reference_spot": round(spot_value, 6) if spot_value is not None else None,
        "expected_move": round(move_value, 6) if move_value is not None else None,
        "max_adverse_move_em": adverse,
        "confirmation_samples": samples,
        "sample_interval_seconds": interval,
    }


def evaluate(policy: dict | None, current_context: dict | None,
             current_spot: float | None) -> dict:
    """Evaluate one current sample without mutating durable trigger state."""
    policy = dict(policy or {})
    mode = str(policy.get("mode") or "")
    if mode == "direction_agnostic":
        return {
            "status": "passed", "conflict": False,
            "reason": "direction-agnostic entry does not require a trend label",
            "mode": mode,
        }
    if mode not in MODES:
        return {"status": "waiting_data", "conflict": False,
                "reason": "entry signal policy is missing or invalid", "mode": mode}

    context = dict(current_context or {})
    label = str(context.get("classification") or "insufficient_data")
    bias = str(policy.get("candidate_bias") or "unknown")
    try:
        spot = float(current_spot)
        reference = float(policy["reference_spot"])
        expected_move = float(policy["expected_move"])
    except (TypeError, ValueError, KeyError):
        return {"status": "waiting_data", "conflict": False,
                "reason": "current spot or expected-move reference is unavailable",
                "mode": mode, "current_classification": label}
    if (not all(math.isfinite(value) for value in (spot, reference, expected_move))
            or spot <= 0 or reference <= 0 or expected_move <= 0):
        return {"status": "waiting_data", "conflict": False,
                "reason": "current spot or expected-move reference is invalid",
                "mode": mode, "current_classification": label}

    sign = 1.0 if bias == "bullish" else -1.0
    signed_move_em = sign * (spot - reference) / expected_move
    max_adverse = float(policy.get("max_adverse_move_em") or 0.15)
    opposite = "bearish" if bias == "bullish" else "bullish"
    adverse_breach = signed_move_em < -max_adverse
    opposite_label = label == opposite
    details = {
        "mode": mode,
        "candidate_bias": bias,
        "current_classification": label,
        "signed_move_from_reference_em": round(signed_move_em, 4),
        "max_adverse_move_em": max_adverse,
        "conflict": False,
    }

    if label == "insufficient_data":
        return {**details, "status": "waiting_data",
                "reason": "current directional context is unavailable"}

    if mode == "momentum_continuation":
        if opposite_label or adverse_breach:
            reasons = []
            if opposite_label:
                reasons.append(f"current market label is {label}, opposite {bias} bias")
            if adverse_breach:
                reasons.append(
                    f"adverse move {signed_move_em:.3f} EM exceeds {-max_adverse:.3f} EM")
            return {**details, "status": "conflicted", "conflict": True,
                    "reason": "; ".join(reasons)}
        if label != bias:
            return {**details, "status": "waiting_signal",
                    "reason": f"momentum entry requires current {bias} label; got {label}"}
        return {**details, "status": "passed",
                "reason": f"current {label} label preserves the continuation thesis"}

    # Pullbacks may enter while the label is neutral, but never after an observed
    # opposite move has exceeded the host-bounded adverse allowance.
    if opposite_label and adverse_breach:
        return {**details, "status": "conflicted", "conflict": True,
                "reason": (f"{label} reversal and {signed_move_em:.3f} EM adverse move "
                           "invalidate the pullback entry")}
    if opposite_label or adverse_breach:
        return {**details, "status": "waiting_signal",
                "reason": ("pullback has not stabilized: opposite label or adverse "
                           "move remains present")}
    return {**details, "status": "passed",
            "reason": "pullback remains bounded without a confirmed reversal"}
