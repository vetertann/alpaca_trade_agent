"""Shadow baselines.

All competition trading happens in one account, so policy comparison runs without
placing competing orders. Fixed policies read the same real-time quotes on the same
clock and mark a virtual book; nothing reaches the broker.

Paper fills price against live quotes with no simulated slippage, so a shadow book
marked at buy-the-ask / sell-the-bid closely approximates what the paper account
would have recorded had that policy been the one trading.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from agent.quant import structures as st
from agent.types import CONTRACT_MULTIPLIER, Leg


@dataclass
class ShadowPosition:
    policy: str
    opened_at: dt.datetime
    legs: list[Leg]
    qty: int
    entry_price: float             # per unit, debit positive
    max_loss: float
    thesis: str
    closed_at: dt.datetime | None = None
    exit_price: float | None = None

    @property
    def open(self) -> bool:
        return self.closed_at is None

    def mark(self, quotes: dict[str, dict]) -> float | None:
        """Mark to market at the price it could be closed at right now."""
        total = 0.0
        for leg in self.legs:
            q = quotes.get(leg.symbol)
            if not q:
                return None
            bid, ask = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
            if bid <= 0 or ask <= 0:
                return None
            # closing reverses the leg: a long leg is sold at the bid
            total += -leg.sign * leg.ratio_qty * (bid if leg.sign > 0 else -ask)
        return total

    def unrealised(self, quotes: dict[str, dict]) -> float | None:
        m = self.mark(quotes)
        if m is None:
            return None
        return (m - self.entry_price) * self.qty * CONTRACT_MULTIPLIER

    def to_json(self) -> dict:
        return {"policy": self.policy, "opened_at": self.opened_at.isoformat(),
                "qty": self.qty, "entry_price": round(self.entry_price, 3),
                "max_loss": round(self.max_loss, 2), "thesis": self.thesis,
                "closed_at": self.closed_at.isoformat() if self.closed_at else None,
                "exit_price": self.exit_price,
                "legs": [l.symbol for l in self.legs]}


class ShadowBook:
    """One policy's virtual account."""

    def __init__(self, policy: str, equity: float = 100_000.0):
        self.policy = policy
        self.starting_equity = equity
        self.cash = equity
        self.positions: list[ShadowPosition] = []
        self.realised = 0.0

    def open_position(self, legs: list[Leg], qty: int, price: float, thesis: str,
                      when: dt.datetime) -> ShadowPosition:
        p = ShadowPosition(self.policy, when, legs, qty, price,
                           st.max_loss(legs, price, qty), thesis)
        self.positions.append(p)
        self.cash -= price * qty * CONTRACT_MULTIPLIER
        return p

    def close_position(self, pos: ShadowPosition, quotes: dict, when: dt.datetime) -> bool:
        m = pos.mark(quotes)
        if m is None:
            return False
        pos.closed_at, pos.exit_price = when, m
        proceeds = m * pos.qty * CONTRACT_MULTIPLIER
        self.cash += proceeds
        self.realised += proceeds - pos.entry_price * pos.qty * CONTRACT_MULTIPLIER
        return True

    def equity(self, quotes: dict) -> float:
        held = 0.0
        for p in self.positions:
            if not p.open:
                continue
            m = p.mark(quotes)
            if m is not None:
                held += m * p.qty * CONTRACT_MULTIPLIER
        return self.cash + held

    def summary(self, quotes: dict) -> dict:
        eq = self.equity(quotes)
        return {"policy": self.policy, "equity": round(eq, 2),
                "return_pct": round((eq / self.starting_equity - 1) * 100, 3),
                "realised": round(self.realised, 2),
                "open": sum(1 for p in self.positions if p.open),
                "total": len(self.positions)}


# ---------------------------------------------------------------- the policies

def _pick(chain: list[dict], kind: str, target: float) -> dict | None:
    rows = [c for c in chain if c["option_type"] == kind]
    return min(rows, key=lambda c: abs(c["strike"] - target)) if rows else None


def _legs(rows: list[dict], sides: list[str]) -> list[Leg]:
    return [Leg(r["symbol"], 1, s, "buy_to_open" if s == "buy" else "sell_to_open",
                r["strike"], r["option_type"], dt.date.fromisoformat(r["expiry"]))
            for r, s in zip(rows, sides)]


def bull_call_spread(chain, spot, width=5.0):
    lo, hi = _pick(chain, "call", spot), _pick(chain, "call", spot + width)
    if not lo or not hi or lo["strike"] >= hi["strike"]:
        return None
    return _legs([lo, hi], ["buy", "sell"]), lo["ask"] - hi["bid"], "fixed bull call spread"


def long_straddle(chain, spot, width=0.0):
    c, p = _pick(chain, "call", spot), _pick(chain, "put", spot)
    if not c or not p:
        return None
    return _legs([c, p], ["buy", "buy"]), c["ask"] + p["ask"], "fixed long straddle"


def credit_put_spread(chain, spot, width=5.0):
    short, long = _pick(chain, "put", spot - width), _pick(chain, "put", spot - 2 * width)
    if not short or not long or short["strike"] <= long["strike"]:
        return None
    return _legs([long, short], ["buy", "sell"]), long["ask"] - short["bid"], \
        "fixed defined-risk credit put spread"


def flat_cash(chain, spot, width=0.0):
    return None                      # the reference line: never trades


POLICIES = {"bull_call": bull_call_spread, "long_straddle": long_straddle,
            "credit_put": credit_put_spread, "flat_cash": flat_cash}


class ShadowRunner:
    """Runs every fixed policy alongside the live agent. Places no orders."""

    def __init__(self, equity: float = 100_000.0, risk_budget: float = 3_000.0,
                 path: str | Path = ".run/shadow.jsonl"):
        self.books = {name: ShadowBook(name, equity) for name in POLICIES}
        self.risk_budget = risk_budget
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def step(self, chain: list[dict], spot: float, quotes: dict,
             when: dt.datetime, *, may_enter: bool) -> dict:
        """One tick. Each policy holds at most one position at a time."""
        for name, fn in POLICIES.items():
            book = self.books[name]
            live = [p for p in book.positions if p.open]
            if live or not may_enter or not chain:
                continue
            built = fn(chain, spot)
            if not built:
                continue
            legs, price, thesis = built
            per_unit = st.max_loss(legs, price, 1)
            if per_unit <= 0:
                continue
            qty = int(self.risk_budget // per_unit)
            if qty >= 1:
                book.open_position(legs, qty, price, thesis, when)
        return {n: b.summary(quotes) for n, b in self.books.items()}

    def close_all(self, quotes: dict, when: dt.datetime) -> None:
        for book in self.books.values():
            for p in [x for x in book.positions if x.open]:
                book.close_position(p, quotes, when)

    def record(self, quotes: dict, when: dt.datetime) -> dict:
        row = {"ts": when.isoformat(),
               "books": {n: b.summary(quotes) for n, b in self.books.items()}}
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row
