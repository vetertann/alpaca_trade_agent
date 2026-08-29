"""Host-side option contract resolution and intent canonicalisation."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from agent.types import Leg, TradeIntent


_OCC = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<date>\d{6})(?P<kind>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class ContractMeta:
    symbol: str
    underlying: str
    strike: float
    option_type: str
    expiry: dt.date
    tradable: bool = True
    status: str = "active"


def parse_occ_symbol(symbol: str) -> ContractMeta:
    """Parse standard OCC symbology without trusting model-supplied metadata."""
    m = _OCC.fullmatch(str(symbol).upper())
    if not m:
        raise ValueError(f"{symbol!r} is not valid OCC option symbology")
    expiry = dt.datetime.strptime(m.group("date"), "%y%m%d").date()
    return ContractMeta(
        symbol=str(symbol).upper(), underlying=m.group("root"),
        strike=int(m.group("strike")) / 1000.0,
        option_type="call" if m.group("kind") == "C" else "put",
        expiry=expiry)


def meta_from_contract(contract: dict) -> ContractMeta:
    symbol = str(contract.get("symbol") or "").upper()
    parsed = parse_occ_symbol(symbol)
    meta = ContractMeta(
        symbol=symbol,
        underlying=str(contract.get("underlying_symbol") or contract.get("root_symbol")
                       or parsed.underlying).upper(),
        strike=float(contract.get("strike_price", parsed.strike)),
        option_type=str(contract.get("type") or parsed.option_type).lower(),
        expiry=dt.date.fromisoformat(str(contract.get("expiration_date") or parsed.expiry)),
        tradable=bool(contract.get("tradable", True)),
        status=str(contract.get("status") or "active"))
    if (meta.underlying != parsed.underlying or meta.strike != parsed.strike
            or meta.option_type != parsed.option_type or meta.expiry != parsed.expiry):
        raise ValueError(f"broker metadata for {symbol} disagrees with OCC symbology")
    return meta


def resolve_intent(rest, intent: TradeIntent) -> TradeIntent:
    """Resolve every leg from Alpaca and reject any model/contract mismatch."""
    resolved: list[Leg] = []
    seen: set[str] = set()
    for supplied in intent.legs:
        if supplied.symbol in seen:
            raise ValueError(f"duplicate leg {supplied.symbol}")
        seen.add(supplied.symbol)
        meta = meta_from_contract(rest.option_contract(supplied.symbol))
        if not meta.tradable or meta.status != "active":
            raise ValueError(f"{meta.symbol} is not an active tradable contract")
        mismatches = []
        if abs(float(supplied.strike) - meta.strike) > 1e-9:
            mismatches.append(f"strike {supplied.strike} != {meta.strike}")
        if supplied.option_type != meta.option_type:
            mismatches.append(f"type {supplied.option_type} != {meta.option_type}")
        if supplied.expiry != meta.expiry:
            mismatches.append(f"expiry {supplied.expiry} != {meta.expiry}")
        if intent.underlying.upper() != meta.underlying:
            mismatches.append(f"underlying {intent.underlying} != {meta.underlying}")
        if mismatches:
            raise ValueError(f"{meta.symbol}: model metadata mismatch: " + "; ".join(mismatches))
        resolved.append(Leg(meta.symbol, int(supplied.ratio_qty), supplied.side,
                            supplied.position_intent, meta.strike,
                            meta.option_type, meta.expiry))
    return TradeIntent(intent.underlying.upper(), intent.family, tuple(resolved),
                       intent.thesis_id, float(intent.risk_budget), intent.note)

