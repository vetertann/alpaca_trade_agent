import datetime as dt

from agent.brain import preflight
from agent.config import ET


class Series:
    def last(self, symbol): return {"SPY": 101.0}.get(symbol)
    def session_range(self, symbol): return (99.0, 102.0) if symbol == "SPY" else None
    def realized_vol(self, symbol): return 0.20 if symbol == "SPY" else None


class Rest:
    def stock_latest_trade(self, symbol): return {"p": 50.0}


def test_confirmation_refresh_replaces_mutable_state_and_hash(monkeypatch):
    monkeypatch.setattr(preflight, "_atm_iv", lambda *args: (0.25, "2026-09-01"))
    old = {
        "bundle_hash": "old", "clock": {"now_et": "old"},
        "account": {"equity": 100_000, "starting_equity": 100_000},
        "book": [], "portfolio": {"equity": 100_000, "structures": []},
        "universe": {"SPY": {"spot": 100.0, "realized_vol": 0.15,
                               "realized_vol_by_window": {"rv5": 0.10}}},
        "theses": [], "execution_control": {}, "expiries": ["2026-09-01"],
    }
    current_portfolio = {"equity": 99_800, "structures": [{"structure_id": "sid-1"}]}
    now = dt.datetime(2026, 8, 31, 14, 15, tzinfo=ET)

    got = preflight.refresh_for_confirmation(
        Rest(), Series(), old,
        account={"equity": 99_800, "cash": 98_000,
                 "options_buying_power": 97_000, "options_trading_level": 3},
        positions=[{"symbol": "OPT", "qty": "1", "side": "long",
                    "market_value": "100", "unrealized_pl": "5", "cost_basis": "95"}],
        portfolio=current_portfolio, expiries=["2026-09-01"], now=now)

    assert got["bundle_hash"] != "old"
    assert got["account"]["equity"] == 99_800
    assert got["book"][0]["symbol"] == "OPT"
    assert got["portfolio"] == current_portfolio
    assert got["universe"]["SPY"]["spot"] == 101.0
    assert got["diff"]["equity_change"] == -200.0
