"""The four Alpaca streams, owned by one process.

A second connection to a feed already held is refused with 406, and the incumbent
survives -- so this process owns every stream and fans data out internally.

Codecs differ by feed and the difference is not cosmetic: the options feed speaks
MessagePack and answers a JSON auth frame with `400 invalid syntax`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import msgpack
import websockets

from agent.config import Profile

EQUITY_URL = "wss://stream.data.alpaca.markets/v2/iex"
OPTION_URL = "wss://stream.data.alpaca.markets/v1beta1/indicative"
NEWS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
TRADE_URL = "wss://paper-api.alpaca.markets/stream"

MAX_EQUITY_SYMBOLS = 30      # measured: 31 -> 405 symbol limit exceeded
MAX_OPTION_SYMBOLS = 200     # measured: 201 -> 405 symbol limit exceeded


@dataclass
class Handlers:
    on_equity_quote: Callable[[str, float, float, dt.datetime], None] | None = None
    on_option_quote: Callable[[str, float, float, dt.datetime], None] | None = None
    on_news: Callable[[dict], None] | None = None
    on_trade_update: Callable[[dict], None] | None = None
    on_error: Callable[[str, Exception], None] | None = None


def _ts(raw) -> dt.datetime:
    if not raw:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(raw, msgpack.Timestamp):
        return raw.to_datetime()
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


class _Feed:
    """One websocket, one codec, reconnecting."""

    def __init__(self, name: str, url: str, profile: Profile, codec: str,
                 handlers: Handlers):
        self.name, self.url, self.profile, self.codec = name, url, profile, codec
        self.h = handlers
        self.subscribed: set[str] = set()
        self.ws = None
        self.connected = asyncio.Event()
        self.messages = 0
        self.last_message_at: dt.datetime | None = None

    def _enc(self, obj) -> bytes | str:
        return msgpack.packb(obj) if self.codec == "msgpack" else json.dumps(obj)

    def _dec(self, raw):
        if isinstance(raw, bytes):
            try:
                return msgpack.unpackb(raw)
            except Exception:
                return json.loads(raw.decode("utf-8", "replace"))
        return json.loads(raw)

    async def _authenticate(self) -> None:
        await self.ws.send(self._enc(
            {"action": "auth", "key": self.profile.api_key,
             "secret": self.profile.secret_key}))

    async def subscribe(self, key: str, symbols: list[str]) -> None:
        if not symbols or self.ws is None:
            return
        await self.ws.send(self._enc({"action": "subscribe", key: symbols}))
        self.subscribed |= set(symbols)

    async def unsubscribe(self, key: str, symbols: list[str]) -> None:
        if not symbols or self.ws is None:
            return
        await self.ws.send(self._enc({"action": "unsubscribe", key: symbols}))
        self.subscribed -= set(symbols)

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("T")
        if t == "q":                                  # quote, equity or option
            sym = msg.get("S", "")
            bid, ask = float(msg.get("bp", 0) or 0), float(msg.get("ap", 0) or 0)
            when = _ts(msg.get("t"))
            cb = self.h.on_option_quote if self.name == "options" else self.h.on_equity_quote
            if cb:
                cb(sym, bid, ask, when)
        elif t == "n" and self.h.on_news:
            self.h.on_news(msg)
        elif t == "error" and self.h.on_error:
            self.h.on_error(self.name, RuntimeError(str(msg)))

    async def run(self, initial: tuple[str, list[str]] | None = None) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, max_size=2 ** 23,
                                              open_timeout=20) as ws:
                    self.ws = ws
                    await self._authenticate()
                    self.connected.set()
                    backoff = 1.0
                    if initial:
                        await self.subscribe(*initial)
                    elif self.subscribed:
                        await self.subscribe("quotes", sorted(self.subscribed))
                    async for raw in ws:
                        self.messages += 1
                        self.last_message_at = dt.datetime.now(dt.timezone.utc)
                        payload = self._dec(raw)
                        for m in (payload if isinstance(payload, list) else [payload]):
                            if isinstance(m, dict):
                                self._dispatch(m)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # noqa: BLE001 - reconnect on anything
                self.connected.clear()
                if self.h.on_error:
                    self.h.on_error(self.name, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


class TradeUpdates(_Feed):
    """Order events. Different auth envelope, binary frames."""

    async def _authenticate(self) -> None:
        await self.ws.send(json.dumps(
            {"action": "auth", "key": self.profile.api_key,
             "secret": self.profile.secret_key}))
        await self.ws.send(json.dumps(
            {"action": "listen", "data": {"streams": ["trade_updates"]}}))

    def _dispatch(self, msg: dict) -> None:
        if msg.get("stream") == "trade_updates" and self.h.on_trade_update:
            self.h.on_trade_update(msg.get("data", {}))


@dataclass
class StreamSet:
    profile: Profile
    handlers: Handlers
    equity: _Feed = field(init=False)
    options: _Feed = field(init=False)
    news: _Feed = field(init=False)
    trades: TradeUpdates = field(init=False)
    _tasks: list = field(default_factory=list, init=False)

    def __post_init__(self):
        self.equity = _Feed("equity", EQUITY_URL, self.profile, "json", self.handlers)
        self.options = _Feed("options", OPTION_URL, self.profile, "msgpack", self.handlers)
        self.news = _Feed("news", NEWS_URL, self.profile, "json", self.handlers)
        self.trades = TradeUpdates("trades", TRADE_URL, self.profile, "json", self.handlers)

    async def start(self, equity_symbols: list[str], option_symbols: list[str],
                    news_symbols: list[str] | None = None) -> None:
        if len(equity_symbols) > MAX_EQUITY_SYMBOLS:
            raise ValueError(f"{len(equity_symbols)} equity symbols exceeds {MAX_EQUITY_SYMBOLS}")
        if len(option_symbols) > MAX_OPTION_SYMBOLS:
            raise ValueError(f"{len(option_symbols)} option symbols exceeds {MAX_OPTION_SYMBOLS}")
        self._tasks = [
            asyncio.create_task(self.equity.run(("quotes", equity_symbols)), name="equity"),
            asyncio.create_task(self.options.run(("quotes", option_symbols)), name="options"),
            asyncio.create_task(self.news.run(("news", news_symbols or ["*"])), name="news"),
            asyncio.create_task(self.trades.run(), name="trades"),
        ]
        await asyncio.wait([asyncio.create_task(f.connected.wait())
                            for f in (self.equity, self.options, self.news, self.trades)],
                           timeout=30)

    async def recentre_options(self, keep: list[str], add: list[str]) -> None:
        """Re-centre the strike window. Subscribe traffic, free against REST budget."""
        drop = sorted(self.options.subscribed - set(keep) - set(add))
        if drop:
            await self.options.unsubscribe("quotes", drop)
        new = [s for s in add if s not in self.options.subscribed]
        if new:
            await self.options.subscribe("quotes", new)

    def unhealthy(self, stale_after_s: float = 900.0) -> list[str]:
        """Feeds that are down, or silent long enough to be suspect."""
        now = dt.datetime.now(dt.timezone.utc)
        out = []
        for f in (self.equity, self.options, self.news, self.trades):
            if not f.connected.is_set():
                out.append(f"{f.name}:disconnected")
            elif f.last_message_at and (now - f.last_message_at).total_seconds() > stale_after_s:
                out.append(f"{f.name}:silent")
        return out

    def status(self) -> dict:
        return {f.name: {"connected": f.connected.is_set(), "messages": f.messages,
                         "symbols": len(f.subscribed),
                         "last": f.last_message_at.isoformat() if f.last_message_at else None}
                for f in (self.equity, self.options, self.news, self.trades)}

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
