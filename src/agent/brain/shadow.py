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
import os
from dataclasses import dataclass, field
from pathlib import Path

from agent.quant import structures as st
from agent.config import ET
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

    @property
    def expiry(self) -> dt.date:
        return min(leg.expiry for leg in self.legs)

    def expired(self, when: dt.datetime) -> bool:
        """Settle at the regular-session close, never at midnight on expiry day."""
        close = dt.datetime.combine(self.expiry, dt.time(16, 0), tzinfo=ET)
        return when.astimezone(ET) >= close

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
            # Value is expressed in the same debit-positive convention as entry:
            # sell a long at bid (+), buy a short back at ask (-).
            total += leg.sign * leg.ratio_qty * (bid if leg.sign > 0 else ask)
        return total

    def unrealised(self, quotes: dict[str, dict]) -> float | None:
        m = self.mark(quotes)
        if m is None:
            return None
        return (m - self.entry_price) * self.qty * CONTRACT_MULTIPLIER

    def to_json(self, *, durable: bool = False) -> dict:
        legs = ([{"symbol": leg.symbol, "ratio_qty": leg.ratio_qty,
                  "side": leg.side, "position_intent": leg.position_intent,
                  "strike": leg.strike, "option_type": leg.option_type,
                  "expiry": leg.expiry.isoformat()} for leg in self.legs]
                if durable else [leg.symbol for leg in self.legs])
        return {"policy": self.policy, "opened_at": self.opened_at.isoformat(),
                "qty": self.qty,
                "entry_price": self.entry_price if durable else round(self.entry_price, 3),
                "max_loss": self.max_loss if durable else round(self.max_loss, 2),
                "thesis": self.thesis,
                "closed_at": self.closed_at.isoformat() if self.closed_at else None,
                "exit_price": self.exit_price,
                "legs": legs}

    @classmethod
    def from_state(cls, raw: dict) -> "ShadowPosition":
        legs = [Leg(symbol=str(leg["symbol"]), ratio_qty=int(leg["ratio_qty"]),
                    side=leg["side"], position_intent=leg["position_intent"],
                    strike=float(leg["strike"]), option_type=leg["option_type"],
                    expiry=dt.date.fromisoformat(leg["expiry"]))
                for leg in raw["legs"]]
        return cls(policy=str(raw["policy"]),
                   opened_at=dt.datetime.fromisoformat(raw["opened_at"]),
                   legs=legs, qty=int(raw["qty"]),
                   entry_price=float(raw["entry_price"]),
                   max_loss=float(raw["max_loss"]), thesis=str(raw["thesis"]),
                   closed_at=(dt.datetime.fromisoformat(raw["closed_at"])
                              if raw.get("closed_at") else None),
                   exit_price=(float(raw["exit_price"])
                               if raw.get("exit_price") is not None else None))


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

    def settle_expired(self, pos: ShadowPosition, spot: float,
                       when: dt.datetime) -> float:
        """Settle at intrinsic value against the underlying, not against a quote."""
        per_unit = st.net_payoff_at(pos.legs, spot) / CONTRACT_MULTIPLIER
        pos.closed_at, pos.exit_price = when, per_unit
        proceeds = per_unit * pos.qty * CONTRACT_MULTIPLIER
        self.cash += proceeds
        self.realised += proceeds - pos.entry_price * pos.qty * CONTRACT_MULTIPLIER
        return proceeds

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

    def to_state(self) -> dict:
        return {"policy": self.policy, "starting_equity": self.starting_equity,
                "cash": self.cash, "realised": self.realised,
                "positions": [p.to_json(durable=True) for p in self.positions]}

    @classmethod
    def from_state(cls, raw: dict) -> "ShadowBook":
        book = cls(str(raw["policy"]), float(raw["starting_equity"]))
        book.cash = float(raw["cash"])
        book.realised = float(raw["realised"])
        book.positions = [ShadowPosition.from_state(p) for p in raw["positions"]]
        return book


# ---------------------------------------------------------------- the policies

def _pick(chain: list[dict], kind: str, target: float) -> dict | None:
    rows = [c for c in chain if c["option_type"] == kind]
    return min(rows, key=lambda c: abs(c["strike"] - target)) if rows else None


def _legs(rows: list[dict], sides: list[str]) -> list[Leg]:
    return [Leg(r["symbol"], 1, s, "buy_to_open" if s == "buy" else "sell_to_open",
                r["strike"], r["option_type"], dt.date.fromisoformat(r["expiry"]))
            for r, s in zip(rows, sides)]


def _expiry_groups(chain: list[dict]):
    """Yield expiry-homogeneous chains, nearest first.

    A spread assembled across expiries is a calendar, with completely different
    risk from the fixed vertical/straddle policy being benchmarked.
    """
    expiries = sorted({str(row["expiry"]) for row in chain})
    for expiry in expiries:
        yield [row for row in chain if str(row["expiry"]) == expiry]


def bull_call_spread(chain, spot, width=5.0):
    for expiry_chain in _expiry_groups(chain):
        lo = _pick(expiry_chain, "call", spot)
        hi = _pick(expiry_chain, "call", spot + width)
        if lo and hi and lo["strike"] < hi["strike"]:
            return (_legs([lo, hi], ["buy", "sell"]), lo["ask"] - hi["bid"],
                    "fixed bull call spread")
    return None


def long_straddle(chain, spot, width=0.0):
    for expiry_chain in _expiry_groups(chain):
        c = _pick(expiry_chain, "call", spot)
        p = _pick(expiry_chain, "put", spot)
        if c and p:
            return (_legs([c, p], ["buy", "buy"]), c["ask"] + p["ask"],
                    "fixed long straddle")
    return None


def credit_put_spread(chain, spot, width=5.0):
    for expiry_chain in _expiry_groups(chain):
        short = _pick(expiry_chain, "put", spot - width)
        long = _pick(expiry_chain, "put", spot - 2 * width)
        if short and long and short["strike"] > long["strike"]:
            return (_legs([long, short], ["buy", "sell"]),
                    long["ask"] - short["bid"],
                    "fixed defined-risk credit put spread")
    return None


def flat_cash(chain, spot, width=0.0):
    return None                      # the reference line: never trades


POLICIES = {"bull_call": bull_call_spread, "long_straddle": long_straddle,
            "credit_put": credit_put_spread, "flat_cash": flat_cash}


class ShadowRunner:
    """Runs every fixed policy alongside the live agent. Places no orders."""

    def __init__(self, equity: float = 100_000.0, risk_budget: float = 3_000.0,
                 path: str | Path = ".run/shadow.jsonl"):
        self.risk_budget = risk_budget
        self.path = Path(path)
        self.state_path = self.path.with_suffix(".state.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.epoch_started_at = dt.datetime.now(dt.timezone.utc)
        self.books = {name: ShadowBook(name, equity) for name in POLICIES}
        if self.state_path.exists():
            self._restore()

    def _restore(self) -> None:
        raw = json.loads(self.state_path.read_text())
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported shadow state schema")
        restored = {name: ShadowBook.from_state(book)
                    for name, book in (raw.get("books") or {}).items()}
        if set(restored) != set(POLICIES):
            raise ValueError("shadow state policy set is incomplete")
        self.books = restored
        self.epoch_started_at = dt.datetime.fromisoformat(raw["epoch_started_at"])

    def _checkpoint(self) -> None:
        payload = {"schema_version": 1,
                   "epoch_started_at": self.epoch_started_at.isoformat(),
                   "risk_budget": self.risk_budget,
                   "books": {name: book.to_state()
                             for name, book in self.books.items()}}
        temp = self.state_path.with_name(f".{self.state_path.name}.tmp")
        with temp.open("w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, self.state_path)
        directory = os.open(self.state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def step(self, chain: list[dict], spot: float, quotes: dict,
             when: dt.datetime, *, may_enter: bool) -> dict:
        """One tick.

        Each policy holds at most one position at a time and re-enters once the
        previous one has expired, so a baseline is the strategy run repeatedly
        across the window rather than a single position bought on Monday.
        """
        self.settle(spot, when)
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

    def settle(self, spot: float, when: dt.datetime) -> list[str]:
        """Settle everything that has reached expiry. Frees each book to re-enter."""
        done = []
        for name, book in self.books.items():
            for p in [x for x in book.positions if x.open and x.expired(when)]:
                book.settle_expired(p, spot, when)
                done.append(f"{name} settled {p.expiry} at {spot:.2f}")
        return done

    def close_all(self, quotes: dict, when: dt.datetime) -> None:
        for book in self.books.values():
            for p in [x for x in book.positions if x.open]:
                book.close_position(p, quotes, when)

    def record(self, quotes: dict, when: dt.datetime) -> dict:
        self._checkpoint()
        row = {"schema_version": 1, "ts": when.isoformat(),
               "epoch_started_at": self.epoch_started_at.isoformat(),
               "books": {n: b.summary(quotes) for n, b in self.books.items()}}
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return row
