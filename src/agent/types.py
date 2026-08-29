"""Core data types.

The model proposes a TradeIntent. The host materialises a VerifiedTradeIntent
from fresh quotes and account state. Only the latter can be executed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, field, asdict
from typing import Literal

Side = Literal["buy", "sell"]
Intent = Literal["buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"]
OptionType = Literal["call", "put"]

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class Leg:
    symbol: str
    ratio_qty: int
    side: Side
    position_intent: Intent
    strike: float
    option_type: OptionType
    expiry: dt.date

    @property
    def sign(self) -> int:
        """+1 long, -1 short."""
        return 1 if self.side == "buy" else -1


@dataclass(frozen=True)
class TradeIntent:
    """What the model proposes. Geometry only -- no executable pricing."""
    underlying: str
    family: str
    legs: tuple[Leg, ...]
    thesis_id: str
    risk_budget: float                 # dollars the model wants at risk
    note: str = ""


@dataclass(frozen=True)
class VerifiedTradeIntent:
    """What the host materialised. The only thing execution accepts."""
    intent: TradeIntent
    qty: int
    limit_price: float                 # net, debit positive
    max_loss: float                    # dollars, whole position
    max_profit: float
    quote_snapshot_hash: str
    materialised_at: dt.datetime
    ttl_seconds: float
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)

    def expired(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        return (now - self.materialised_at).total_seconds() > self.ttl_seconds

    def client_order_id(self) -> str:
        """Deterministic: a retry after an ambiguous failure collides, not duplicates."""
        legs = "|".join(f"{l.symbol}:{l.sign * l.ratio_qty}" for l in self.intent.legs)
        raw = f"{self.intent.thesis_id}/{legs}/{self.qty}/{self.nonce}"
        return "a" + hashlib.sha256(raw.encode()).hexdigest()[:31]


@dataclass(frozen=True)
class GateResult:
    """Pure verdict over plain data. Rendered directly into the PASS/FAIL checklist."""
    name: str
    passed: bool
    reason: str

    def render(self) -> str:
        return f"{'PASS' if self.passed else 'FAIL'}  {self.name}: {self.reason}"


CycleOutcome = Literal[
    "EXECUTED", "NO_TRADE", "BLOCKED_RISK", "BLOCKED_LIQUIDITY", "DEGRADED", "ERROR"
]


@dataclass
class Thesis:
    thesis_id: str
    opened_at: dt.datetime
    hypothesis: str
    exit_profit: str
    exit_invalidation: str
    exit_time: str
    exit_news: str
    evidence_refs: list[str] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)   # YES / NO / BLOCKED
    status: str = "open"
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["opened_at"] = self.opened_at.isoformat()
        return d
