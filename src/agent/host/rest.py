"""Alpaca REST access. The host owns credentials; the sandbox never sees them."""
from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from agent.config import DATA_URL, PAPER_TRADING_URL, Profile, assert_paper
from agent.host.limiter import DATA, TRADING


class Rest:
    def __init__(self, profile: Profile, timeout: float = 30.0):
        assert_paper(PAPER_TRADING_URL)
        self.profile = profile
        self._h = {"APCA-API-KEY-ID": profile.api_key,
                   "APCA-API-SECRET-KEY": profile.secret_key}
        self._c = httpx.Client(timeout=timeout, headers=self._h)
        self._contract_cache: dict[str, list[dict]] = {}
        self._single_contract_cache: dict[str, dict] = {}

    # ---- plumbing ----------------------------------------------------------
    def _get(self, base: str, path: str, bucket, **params) -> dict:
        bucket.take()
        r = self._c.get(f"{base}{path}", params={k: v for k, v in params.items()
                                                 if v is not None})
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        TRADING.take()
        r = self._c.post(f"{PAPER_TRADING_URL}{path}", json=body)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(f"{r.status_code} {r.text[:300]}",
                                        request=r.request, response=r)
        return r.json()

    def _paged(self, base, path, bucket, key, **params) -> list[dict]:
        out, token = [], None
        while True:
            page = self._get(base, path, bucket, page_token=token, **params)
            rows = page.get(key) or []
            out += rows
            token = page.get("next_page_token")
            if not token or not rows:
                return out

    # ---- account -----------------------------------------------------------
    def account(self) -> dict:
        return self._get(PAPER_TRADING_URL, "/v2/account", TRADING)

    def positions(self) -> list[dict]:
        return self._get(PAPER_TRADING_URL, "/v2/positions", TRADING)  # type: ignore[return-value]

    def orders(self, status: str = "open") -> list[dict]:
        return self._get(PAPER_TRADING_URL, "/v2/orders", TRADING, status=status)  # type: ignore[return-value]

    def clock(self) -> dict:
        return self._get(PAPER_TRADING_URL, "/v2/clock", TRADING)

    # ---- contracts (cached for the session; listings change overnight) ------
    def contracts(self, underlying: str, exp_gte: str, exp_lte: str,
                  refresh: bool = False) -> list[dict]:
        key = f"{underlying}:{exp_gte}:{exp_lte}"
        if refresh or key not in self._contract_cache:
            self._contract_cache[key] = self._paged(
                PAPER_TRADING_URL, "/v2/options/contracts", TRADING, "option_contracts",
                underlying_symbols=underlying, status="active", limit=10000,
                expiration_date_gte=exp_gte, expiration_date_lte=exp_lte)
        return self._contract_cache[key]

    def option_contract(self, symbol_or_id: str) -> dict:
        if symbol_or_id not in self._single_contract_cache:
            self._single_contract_cache[symbol_or_id] = self._get(
                PAPER_TRADING_URL, f"/v2/options/contracts/{symbol_or_id}", TRADING)
        return self._single_contract_cache[symbol_or_id]

    # ---- market data -------------------------------------------------------
    def stock_latest_trade(self, symbol: str) -> dict:
        return self._get(DATA_URL, f"/v2/stocks/{symbol}/trades/latest", DATA)["trade"]

    def stock_bars(self, symbol: str, timeframe: str, start: str, end: str) -> list[dict]:
        return self._get(DATA_URL, "/v2/stocks/bars", DATA, symbols=symbol,
                         timeframe=timeframe, start=start, end=end,
                         limit=10000).get("bars", {}).get(symbol, [])

    def option_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Explicitly named symbols. `snapshots` paginates in symbol order and can
        silently return an unintended expiry."""
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            out |= self._get(DATA_URL, "/v1beta1/options/quotes/latest", DATA,
                             symbols=",".join(chunk)).get("quotes", {})
        return out

    def option_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Carries Alpaca's Greeks and IV where a valid two-sided quote exists."""
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            out |= self._get(DATA_URL, "/v1beta1/options/snapshots", DATA,
                             symbols=",".join(chunk)).get("snapshots", {})
        return out

    def corporate_actions(self, symbols: list[str], start: str, end: str) -> dict:
        """Announced actions. A dividend ex-date inside the window raises early
        assignment risk on any in-the-money short call."""
        return self._get(DATA_URL, "/v1beta1/corporate-actions", DATA,
                         symbols=",".join(symbols), start=start, end=end,
                         limit=200).get("corporate_actions", {})

    def news(self, symbols: list[str] | None, start: str, limit: int = 50) -> list[dict]:
        return self._get(DATA_URL, "/v1beta1/news", DATA,
                         symbols=",".join(symbols) if symbols else None,
                         start=start, limit=limit, sort="desc").get("news", [])

    # ---- orders ------------------------------------------------------------
    def submit_mleg(self, legs: list[dict], qty: int, limit_price: float,
                    client_order_id: str, tif: str = "day") -> dict:
        return self._post("/v2/orders", {
            "order_class": "mleg", "qty": str(qty), "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": tif,
            "client_order_id": client_order_id, "legs": legs})

    def submit_single(self, symbol: str, qty: int, side: str, intent: str,
                      limit_price: float, client_order_id: str,
                      tif: str = "day") -> dict:
        return self._post("/v2/orders", {
            "symbol": symbol, "qty": str(qty), "side": side, "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": tif,
            "position_intent": intent, "client_order_id": client_order_id})

    def cancel(self, order_id: str) -> None:
        TRADING.take()
        r = self._c.delete(f"{PAPER_TRADING_URL}/v2/orders/{order_id}")
        if r.status_code not in (200, 204):
            r.raise_for_status()

    def order(self, order_id: str) -> dict:
        return self._get(PAPER_TRADING_URL, f"/v2/orders/{order_id}", TRADING)

    def close(self) -> None:
        self._c.close()
