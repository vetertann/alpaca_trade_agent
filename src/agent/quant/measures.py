"""Real-world probability measures.

Risk-neutral pricing assigns roughly zero expected return to every candidate, so
any edge comes from a real-world distribution we supply. A single distribution can
manufacture edge, and at zero-to-five days the tail dominates -- which matters most
in the comparison we care about, since long gamma and defined-risk credit carry
opposite tail exposures.

Three independent measures, and a candidate is judged on whether it survives all
three.
"""
from __future__ import annotations

import math
import random
import statistics as stats
from dataclasses import dataclass

TRADING_DAYS = 252


@dataclass(frozen=True)
class Measure:
    """A terminal-price sampler for one underlying at one horizon."""
    name: str
    samples: tuple[float, ...]

    def prob(self, predicate) -> float:
        return sum(1 for s in self.samples if predicate(s)) / len(self.samples)

    def expected(self, fn) -> float:
        return sum(fn(s) for s in self.samples) / len(self.samples)


def _daily_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


# ---------------------------------------------------------------- A: lognormal

def lognormal(spot: float, sigma_annual: float, days: float, *, n: int = 20_000,
              drift: float = 0.0, skew: float = 0.0, seed: int = 7) -> Measure:
    """EWMA realized volatility under a lognormal terminal distribution.

    `skew` shifts the downside: index returns are left-skewed, so a positive value
    fattens losses relative to the symmetric case.
    """
    rng = random.Random(seed)
    t = days / TRADING_DAYS
    sd = sigma_annual * math.sqrt(t)
    mu = (drift - 0.5 * sigma_annual ** 2) * t
    out = []
    for _ in range(n):
        z = rng.gauss(0.0, 1.0)
        if skew and z < 0:
            z *= (1.0 + skew)
        out.append(spot * math.exp(mu + sd * z))
    return Measure("lognormal", tuple(out))


# ------------------------------------------------- B: empirical block bootstrap

def block_bootstrap(spot: float, closes: list[float], days: int, *, n: int = 20_000,
                    block: int = 5, seed: int = 11) -> Measure | None:
    """Resampled blocks of observed returns. Assumes no shape, keeps clustering."""
    rets = _daily_returns(closes)
    if len(rets) < block * 4:
        return None
    rng = random.Random(seed)
    horizon = max(int(days), 1)
    out = []
    for _ in range(n):
        total = 0.0
        remaining = horizon
        while remaining > 0:
            start = rng.randrange(0, len(rets) - block + 1)
            take = min(block, remaining)
            total += sum(rets[start:start + take])
            remaining -= take
        out.append(spot * math.exp(total))
    return Measure("block_bootstrap", tuple(out))


# ------------------------------------------------------------------ C: fat tail

def student_t(spot: float, sigma_annual: float, days: float, *, df: float = 4.0,
              n: int = 20_000, seed: int = 13) -> Measure:
    """Student-t innovations, rescaled to the same volatility.

    Prices the tail that a lognormal structurally understates.
    """
    rng = random.Random(seed)
    t = days / TRADING_DAYS
    sd = sigma_annual * math.sqrt(t)
    scale = math.sqrt((df - 2.0) / df) if df > 2 else 1.0
    out = []
    for _ in range(n):
        # t via normal / sqrt(chi2/df); chi2(df) as a sum of squared normals
        z = rng.gauss(0.0, 1.0)
        chi = sum(rng.gauss(0.0, 1.0) ** 2 for _ in range(int(df)))
        tv = z / math.sqrt(chi / df) * scale
        out.append(spot * math.exp(-0.5 * sd * sd + sd * tv))
    return Measure("student_t", tuple(out))


# ---------------------------------------------------------------- the ensemble

def build(spot: float, sigma_annual: float, days: float,
          closes: list[float] | None = None, *, skew: float = 0.15) -> list[Measure]:
    """The three measures, skipping any the data cannot support."""
    out = [lognormal(spot, sigma_annual, days, skew=skew),
           student_t(spot, sigma_annual, days)]
    if closes:
        bs = block_bootstrap(spot, closes, days)
        if bs:
            out.insert(1, bs)
    return out


def evaluate(payoff_fn, measures: list[Measure], traded_price: float, *,
             max_loss: float | None = None, days: float = 1.0) -> dict:
    """Edge under each measure, plus the agreement between them.

    `payoff_fn(spot) -> dollars` at the declared evaluation horizon. It may be
    expiry payoff or a residual-time score mark. `traded_price` is what entering
    costs at buy-the-ask and sell-the-bid.
    """
    per: dict[str, float] = {}
    profits: dict[str, float] = {}
    risk_normalized: dict[str, float] = {}
    capital_day: dict[str, float] = {}
    horizon = max(float(days), 1.0)
    for m in measures:
        ev = m.expected(payoff_fn)
        profit = ev - traded_price
        profits[m.name] = round(profit, 4)
        per[m.name] = round(profit / abs(traded_price), 4) if traded_price else 0.0
        if max_loss is not None and float(max_loss) > 0:
            normalized = profit / float(max_loss)
            risk_normalized[m.name] = round(normalized, 6)
            capital_day[m.name] = round(normalized / horizon, 6)
    values = list(per.values())
    positive = sum(1 for v in values if v > 0)
    out = {
        "expected_profit_by_measure": profits,
        "edge_by_measure": per,
        "edge_min": round(min(values), 4),
        "edge_median": round(stats.median(values), 4),
        "agreement": round(positive / len(values), 3),
        "survives_all": positive == len(values),
    }
    if risk_normalized:
        out.update({
            "risk_normalized_edge_by_measure": risk_normalized,
            "risk_normalized_edge_median": round(
                stats.median(risk_normalized.values()), 6),
            "capital_day_score_by_measure": capital_day,
            "capital_day_score_median": round(
                stats.median(capital_day.values()), 6),
            "capital_day_score_basis": (
                "expected profit / maximum loss / max(days_to_evaluation, 1)"),
        })
    return out


def rank_stability(candidates: list[dict], measures: list[Measure],
                   payoff_of, price_of, top_k: int = 3, *,
                   max_loss_of=None, days_of=None) -> dict:
    """How much a ranking depends on which distribution produced it.

    A candidate attractive under only one convenient distribution is a modelling
    artifact, not edge.
    """
    ranks: dict[str, list[int]] = {}
    scores: dict[str, list[float]] = {}

    def score(candidate: dict, measure: Measure) -> float:
        expected_profit = measure.expected(payoff_of(candidate)) - price_of(candidate)
        if max_loss_of is None:
            return expected_profit
        max_loss = float(max_loss_of(candidate))
        if max_loss <= 0:
            return float("-inf")
        days = max(float(days_of(candidate) if days_of else 1.0), 1.0)
        return expected_profit / max_loss / days

    for m in measures:
        measured = {c["id"]: score(c, m) for c in candidates}
        scored = sorted(candidates, key=lambda c: measured[c["id"]], reverse=True)
        for pos, c in enumerate(scored):
            ranks.setdefault(c["id"], []).append(pos)
            scores.setdefault(c["id"], []).append(measured[c["id"]])
    tops = [set(cid for cid, r in ranks.items() if r[i] < top_k)
            for i in range(len(measures))]
    common = set.intersection(*tops) if tops else set()
    union = set.union(*tops) if tops else set()
    return {"basis": ("expected_profit_per_max_loss_day" if max_loss_of
                       else "expected_profit_dollars"),
            "ranks": ranks,
            "score_median": {cid: round(stats.median(values), 6)
                             for cid, values in scores.items()},
            "stable_top": sorted(common),
            "stability": round(len(common) / len(union), 3) if union else 0.0}
