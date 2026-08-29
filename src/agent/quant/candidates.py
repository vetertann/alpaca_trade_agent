"""Deterministic structure enumeration.

Strikes are chosen in code, not by the model. Models are unreliable at strike
arithmetic and expiry maths and good at deciding which ranked candidate fits a
thesis, so the search is deterministic and the judgement is not.

Economics are computed at buy-the-ask and sell-the-bid before a candidate is ever
shown, so nothing that fails on arithmetic reaches the ranking.
"""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

from agent.quant import structures as st
from agent.types import CONTRACT_MULTIPLIER, Leg

FAMILIES = ("vertical_call", "vertical_put", "straddle", "strangle", "iron_condor")


@dataclass
class Candidate:
    id: str
    family: str
    underlying: str
    expiry: str
    legs: list[Leg]
    net_price: float          # per unit, debit positive
    max_loss: float           # per unit, dollars
    max_profit: float
    width: float
    spread_cost_pct: float    # round-trip crossing cost as a share of max loss
    detail: dict = field(default_factory=dict)

    @property
    def risk_reward(self) -> float:
        return self.max_profit / self.max_loss if self.max_loss > 0 else 0.0

    def to_json(self) -> dict:
        return {"id": self.id, "family": self.family, "underlying": self.underlying,
                "expiry": self.expiry, "net_price": round(self.net_price, 3),
                "max_loss": round(self.max_loss, 2), "max_profit": (
                    None if self.max_profit == st.UNBOUNDED else round(self.max_profit, 2)),
                "risk_reward": round(self.risk_reward, 3),
                "width": self.width, "spread_cost_pct": round(self.spread_cost_pct, 3),
                "legs": [{"symbol": l.symbol, "ratio_qty": l.ratio_qty, "side": l.side,
                          "position_intent": l.position_intent, "strike": l.strike,
                          "option_type": l.option_type, "expiry": l.expiry.isoformat()}
                         for l in self.legs],
                **self.detail}


def _leg(row: dict, side: str) -> Leg:
    return Leg(symbol=row["symbol"], ratio_qty=1, side=side,
               position_intent="buy_to_open" if side == "buy" else "sell_to_open",
               strike=float(row["strike"]), option_type=row["option_type"],
               expiry=dt.date.fromisoformat(row["expiry"]))


def _price(rows: list[dict], sides: list[str]) -> float:
    """Conservative entry: buy at the ask, sell at the bid."""
    return sum((r["ask"] if s == "buy" else -r["bid"]) for r, s in zip(rows, sides))


def _mid_price(rows: list[dict], sides: list[str]) -> float:
    return sum((r["mid"] if s == "buy" else -r["mid"]) for r, s in zip(rows, sides))


def _build(cid: str, family: str, underlying: str, rows: list[dict],
           sides: list[str]) -> Candidate | None:
    legs = [_leg(r, s) for r, s in zip(rows, sides)]
    net = _price(rows, sides)
    max_loss = st.max_loss(legs, net, 1)
    max_profit = st.max_profit(legs, net, 1)
    if max_loss <= 0:
        return None
    if max_profit != st.UNBOUNDED and max_profit <= 0:
        return None                       # dominated: a loss at every outcome
    crossing = abs(net - _mid_price(rows, sides)) * CONTRACT_MULTIPLIER * 2
    return Candidate(cid, family, underlying, rows[0]["expiry"], legs, net,
                     max_loss, max_profit, st.strike_width(legs),
                     crossing / max_loss * 100 if max_loss else 0.0)


def _split(chain: list[dict], expiry: str) -> tuple[list[dict], list[dict]]:
    rows = [r for r in chain if r["expiry"] == expiry]
    calls = sorted((r for r in rows if r["option_type"] == "call"), key=lambda r: r["strike"])
    puts = sorted((r for r in rows if r["option_type"] == "put"), key=lambda r: r["strike"])
    return calls, puts


def enumerate_structures(chain: list[dict], spot: float, *, underlying: str = "SPY",
                         families: tuple[str, ...] = FAMILIES,
                         widths: tuple[float, ...] = (1, 2, 3, 5, 10),
                         moneyness: float = 0.02,
                         max_per_family: int = 40) -> list[Candidate]:
    """Every structure the chain supports, filtered to what is economically sane."""
    out: list[Candidate] = []
    band = spot * moneyness
    for expiry in sorted({r["expiry"] for r in chain}):
        calls, puts = _split(chain, expiry)
        near_c = [r for r in calls if abs(r["strike"] - spot) <= band * 3]
        near_p = [r for r in puts if abs(r["strike"] - spot) <= band * 3]

        if "vertical_call" in families:
            out += _verticals(near_c, "call", widths, underlying, expiry, max_per_family)
        if "vertical_put" in families:
            out += _verticals(near_p, "put", widths, underlying, expiry, max_per_family)
        if "straddle" in families:
            out += _straddles(near_c, near_p, spot, band, underlying, expiry)
        if "strangle" in families:
            out += _strangles(near_c, near_p, spot, band, underlying, expiry)
        if "iron_condor" in families:
            out += _condors(near_c, near_p, spot, widths, underlying, expiry,
                            max_per_family)
    return out


def _verticals(rows, kind, widths, underlying, expiry, cap) -> list[Candidate]:
    out = []
    by_strike = {r["strike"]: r for r in rows}
    for lo in rows:
        for w in widths:
            hi = by_strike.get(lo["strike"] + w)
            if not hi:
                continue
            # debit: long the lower strike (calls) or the higher strike (puts)
            long_row, short_row = (lo, hi) if kind == "call" else (hi, lo)
            c = _build(f"{kind[0]}v_deb_{expiry}_{lo['strike']:.0f}_{w:.0f}",
                       f"vertical_{kind}", underlying, [long_row, short_row],
                       ["buy", "sell"])
            if c:
                out.append(c)
            c = _build(f"{kind[0]}v_cred_{expiry}_{lo['strike']:.0f}_{w:.0f}",
                       f"vertical_{kind}", underlying, [short_row, long_row],
                       ["buy", "sell"])
            if c:
                out.append(c)
            if len(out) >= cap:
                return out
    return out


def _straddles(calls, puts, spot, band, underlying, expiry) -> list[Candidate]:
    out = []
    for c in calls:
        if abs(c["strike"] - spot) > band:
            continue
        p = next((x for x in puts if x["strike"] == c["strike"]), None)
        if not p:
            continue
        cand = _build(f"strad_{expiry}_{c['strike']:.0f}", "straddle", underlying,
                      [c, p], ["buy", "buy"])
        if cand:
            out.append(cand)
    return out


def _strangles(calls, puts, spot, band, underlying, expiry) -> list[Candidate]:
    out = []
    otm_c = [c for c in calls if band < c["strike"] - spot <= band * 3]
    otm_p = [p for p in puts if band < spot - p["strike"] <= band * 3]
    for c, p in itertools.product(otm_c[:6], otm_p[:6]):
        cand = _build(f"strang_{expiry}_{p['strike']:.0f}_{c['strike']:.0f}",
                      "strangle", underlying, [c, p], ["buy", "buy"])
        if cand:
            out.append(cand)
    return out


def _condors(calls, puts, spot, widths, underlying, expiry, cap) -> list[Candidate]:
    out = []
    ck = {r["strike"]: r for r in calls}
    pk = {r["strike"]: r for r in puts}
    short_cs = [c for c in calls if 0 < c["strike"] - spot <= spot * 0.03]
    short_ps = [p for p in puts if 0 < spot - p["strike"] <= spot * 0.03]
    for sc, sp in itertools.product(short_cs[:5], short_ps[:5]):
        for w in widths:
            lc, lp = ck.get(sc["strike"] + w), pk.get(sp["strike"] - w)
            if not lc or not lp:
                continue
            cand = _build(
                f"ic_{expiry}_{sp['strike']:.0f}_{sc['strike']:.0f}_{w:.0f}",
                "iron_condor", underlying, [lp, sp, sc, lc],
                ["buy", "sell", "sell", "buy"])
            if cand:
                out.append(cand)
            if len(out) >= cap:
                return out
    return out


def filter_candidates(cands: list[Candidate], *, min_risk_reward: float = 0.25,
                      max_spread_cost_pct: float = 40.0,
                      max_loss_cap: float | None = None) -> list[Candidate]:
    out = []
    for c in cands:
        if c.max_profit != st.UNBOUNDED and c.risk_reward < min_risk_reward:
            continue
        if c.spread_cost_pct > max_spread_cost_pct:
            continue
        if max_loss_cap is not None and c.max_loss > max_loss_cap:
            continue
        out.append(c)
    return out
