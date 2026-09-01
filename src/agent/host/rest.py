"""Alpaca REST access. The host owns credentials; the sandbox never sees them."""
from __future__ import annotations

import datetime as dt
import os
from typing import Any
from urllib.parse import urlencode

import httpx

from agent.config import DATA_URL, PAPER_TRADING_URL, Profile, assert_paper
from agent.host.alpaca_cli import AlpacaCLI
from agent.host.limiter import DATA, TRADING


class Rest:
    def __init__(self, profile: Profile, timeout: float = 30.0,
                 execution_transport: str | None = None):
        assert_paper(PAPER_TRADING_URL)
        self.profile = profile
        self._h = {"APCA-API-KEY-ID": profile.api_key,
                   "APCA-API-SECRET-KEY": profile.secret_key}
        self._c = httpx.Client(timeout=timeout, headers=self._h)
        self._contract_cache: dict[str, list[dict]] = {}
        self._single_contract_cache: dict[str, dict] = {}
        transport = execution_transport or os.environ.get(
            "ALPACA_EXECUTION_TRANSPORT", "rest")
        if transport not in ("rest", "cli"):
            raise ValueError("ALPACA_EXECUTION_TRANSPORT must be 'rest' or 'cli'")
        self.execution_transport = transport
        self._cli = AlpacaCLI(profile, timeout_s=int(timeout)) if transport == "cli" else None

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

    def _execution_request(self, method: str, path: str, *, body: dict | None = None,
                           **params) -> Any:
        """Route account/order lifecycle calls through the selected transport."""
        if self._cli is None:
            if method == "GET":
                return self._get(PAPER_TRADING_URL, path, TRADING, **params)
            if method == "POST":
                return self._post(path, body or {})
            if method == "DELETE":
                TRADING.take()
                response = self._c.delete(f"{PAPER_TRADING_URL}{path}")
                if response.status_code not in (200, 204):
                    response.raise_for_status()
                return response.json() if response.content else None
            raise ValueError(f"unsupported execution method {method!r}")
        TRADING.take()
        query = urlencode({key: value for key, value in params.items()
                           if value is not None}) or None
        return self._cli.request(method, path, body=body, query=query)

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
        return self._execution_request("GET", "/v2/account")

    def positions(self) -> list[dict]:
        return self._execution_request("GET", "/v2/positions")

    def orders(self, status: str = "open", *, nested: bool | None = None,
               direction: str | None = None, limit: int | None = None) -> list[dict]:
        return self._execution_request(
            "GET", "/v2/orders", status=status, nested=nested,
            direction=direction, limit=limit)

    def order_by_client_order_id(self, client_order_id: str) -> dict:
        return self._execution_request(
            "GET", "/v2/orders:by_client_order_id",
            client_order_id=client_order_id)

    def clock(self) -> dict:
        return self._execution_request("GET", "/v2/clock")

    # ---- contracts (cached for the session; listings change overnight) ------
    def contracts(self, underlying: str, exp_gte: str, exp_lte: str | None = None,
                  refresh: bool = False) -> list[dict]:
        key = f"{underlying}:{exp_gte}:{exp_lte}"
        complete_key = f"{underlying}:{exp_gte}:None"
        if (not refresh and exp_lte is not None
                and complete_key in self._contract_cache):
            # Expiry discovery already downloaded the complete active catalogue.
            # Reuse it for broad or single-tenor scans instead of paging the same
            # 13k SPY contracts again under a differently-shaped cache key.
            if key not in self._contract_cache:
                self._contract_cache[key] = [
                    row for row in self._contract_cache[complete_key]
                    if str(row.get("expiration_date")) <= str(exp_lte)]
            return self._contract_cache[key]
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

    def stock_trades(self, symbols: list[str], start: str, end: str,
                     *, limit: int = 10000) -> dict[str, list[dict]]:
        """Historical prints, paginated without pretending they are quotes."""
        return self._historical_trades(
            "/v2/stocks/trades", symbols, start, end, limit=limit)

    def option_trades(self, symbols: list[str], start: str, end: str,
                      *, limit: int = 10000) -> dict[str, list[dict]]:
        """Historical option prints. Alpaca has no matching historical quote route."""
        return self._historical_trades(
            "/v1beta1/options/trades", symbols, start, end, limit=limit)

    def _historical_trades(self, path: str, symbols: list[str], start: str,
                           end: str, *, limit: int) -> dict[str, list[dict]]:
        out = {symbol: [] for symbol in symbols}
        for offset in range(0, len(symbols), 100):
            chunk = symbols[offset:offset + 100]
            token = None
            while True:
                page = self._get(
                    DATA_URL, path, DATA, symbols=",".join(chunk), start=start,
                    end=end, limit=limit, page_token=token)
                rows = page.get("trades") or {}
                for symbol in chunk:
                    out[symbol].extend(rows.get(symbol) or [])
                token = page.get("next_page_token")
                if not token:
                    break
        return out

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
    def submit_order_body(self, body: dict) -> dict:
        """Submit an already materialized request; recovery can replay it exactly."""
        return self._execution_request("POST", "/v2/orders", body=body)

    def submit_mleg(self, legs: list[dict], qty: int, limit_price: float,
                    client_order_id: str, tif: str = "day") -> dict:
        return self.submit_order_body({
            "order_class": "mleg", "qty": str(qty), "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": tif,
            "client_order_id": client_order_id, "legs": legs})

    def submit_single(self, symbol: str, qty: int, side: str, intent: str,
                      limit_price: float, client_order_id: str,
                      tif: str = "day") -> dict:
        return self.submit_order_body({
            "symbol": symbol, "qty": str(qty), "side": side, "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": tif,
            "position_intent": intent, "client_order_id": client_order_id})

    def cancel(self, order_id: str) -> None:
        self._execution_request("DELETE", f"/v2/orders/{order_id}")

    def order(self, order_id: str) -> dict:
        return self._execution_request("GET", f"/v2/orders/{order_id}")

    def close(self) -> None:
        self._c.close()
