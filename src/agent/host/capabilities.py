"""Capability dispatch.

Maps namespace calls arriving from the sandbox onto real implementations, with
the policy verifier in front of anything that touches the broker. This is the
only path from generated code to the outside world.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from agent.config import ET
from agent.host import telemetry
from agent.host.execution import Executor
from agent.host.rest import Rest
from agent.host.risk_params import RiskParams
from agent.host.series import RollingSeries
from agent.host.thesis_store import ThesisStore
from agent.quant import bs, candidates as cand, measures as ms, vol
from agent.quant import structures as st
from agent.types import Leg, TradeIntent


class CapabilityError(RuntimeError):
    pass


def _leg_from_dict(d: dict) -> Leg:
    return Leg(symbol=d["symbol"], ratio_qty=int(d.get("ratio_qty", 1)),
               side=d["side"], position_intent=d["position_intent"],
               strike=float(d["strike"]), option_type=d["option_type"],
               expiry=dt.date.fromisoformat(str(d["expiry"])))


def _diverse(cands: list, limit: int) -> list:
    """Round-robin across families.

    Ranking by risk/reward puts every unbounded-profit structure first, because
    `inf` always wins, and the model then never sees a vertical or a condor.
    Cheapest-to-cross first within each family, sampled evenly between them.
    """
    by_family: dict[str, list] = {}
    for c in sorted(cands, key=lambda c: c.spread_cost_pct):
        by_family.setdefault(c.family, []).append(c)
    out, i = [], 0
    while len(out) < limit and any(len(v) > i for v in by_family.values()):
        for fam in sorted(by_family):
            if len(by_family[fam]) > i and len(out) < limit:
                out.append(by_family[fam][i])
        i += 1
    return out


def _intent_from_dict(d: dict) -> TradeIntent:
    return TradeIntent(underlying=d["underlying"], family=d.get("family", "custom"),
                       legs=tuple(_leg_from_dict(x) for x in d["legs"]),
                       thesis_id=d["thesis_id"],
                       risk_budget=float(d.get("risk_budget", 0.0)),
                       note=d.get("note", ""))


class Capabilities:
    """One instance per decision cycle."""

    def __init__(self, rest: Rest, series: RollingSeries, theses: ThesisStore,
                 executor: Executor, params: RiskParams, *, equity: float,
                 open_positions: list | None = None, realised_loss: float = 0.0,
                 open_premium_at_risk: float = 0.0):
        self.rest, self.series, self.theses = rest, series, theses
        self.ex, self.params = executor, params
        self.equity = equity
        self.open_positions = open_positions or []
        self.realised_loss = realised_loss
        self.open_premium_at_risk = open_premium_at_risk
        self._contracts: dict[str, dict] = {}
        self._measures: dict[str, list] = {}      # handles; samples never cross the pipe
        self._candidates: dict[str, cand.Candidate] = {}

    # ---- dispatch ----------------------------------------------------------
    def dispatch(self, ns: str, fn: str, args: list, kwargs: dict) -> Any:
        handler = getattr(self, f"_{ns}_{fn}", None)
        if handler is None:
            raise AttributeError(
                f"{ns}.{fn} does not exist. See the capability list in the preamble.")
        tool = f"{ns}.{fn}"
        with telemetry.execute_tool(tool, telemetry.new_call_id(),
                                    arguments={"args": args, "kwargs": kwargs}) as span:
            try:
                return handler(*args, **kwargs)
            except Exception as exc:
                telemetry.record_error(span, exc)
                raise

    # ---- market ------------------------------------------------------------
    def _market_latest_quote(self, symbols):
        syms = [symbols] if isinstance(symbols, str) else list(symbols)
        return {s: {"mid": self.series.last(s)} for s in syms}

    def _market_bars(self, symbol, timeframe="1Day", start=None, end=None):
        """Basic cannot pull the most recent 15 minutes, so `end` defaults behind it."""
        if end is None:
            end = (dt.datetime.now(dt.timezone.utc)
                   - dt.timedelta(minutes=20)).isoformat(timespec="seconds")
        start = start or (dt.date.today() - dt.timedelta(days=120)).isoformat()
        return self.rest.stock_bars(symbol, timeframe, start, end)

    def _market_spot(self, symbol):
        live = self.series.last(symbol)
        return live if live else float(self.rest.stock_latest_trade(symbol)["p"])

    def _market_session_range(self, symbol):
        r = self.series.session_range(symbol)
        return {"low": r[0], "high": r[1]} if r else None

    def _market_news(self, symbols=None, limit=20):
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        return self.rest.news(symbols, start, limit)

    # ---- options -----------------------------------------------------------
    def _options_contracts(self, underlying, exp_gte, exp_lte):
        rows = self.rest.contracts(underlying, exp_gte, exp_lte)
        for c in rows:
            self._contracts[c["symbol"]] = c
        return rows

    def _options_chain(self, underlying, exp_gte, exp_lte, around=None, width=10):
        """Contracts joined to live quotes, restricted to a strike band."""
        spot = around or self._market_spot(underlying)
        rows = [c for c in self._options_contracts(underlying, exp_gte, exp_lte)
                if abs(float(c["strike_price"]) - spot) <= width]
        quotes = self.rest.option_quotes([c["symbol"] for c in rows])
        out = []
        for c in rows:
            q = quotes.get(c["symbol"])
            if not q:
                continue
            bid, ask = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
            if bid <= 0 or ask <= 0:
                continue                       # zero bid means no exit exists
            out.append({"symbol": c["symbol"], "strike": float(c["strike_price"]),
                        "option_type": c["type"], "expiry": c["expiration_date"],
                        "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                        "spread_pct": (ask - bid) / ((bid + ask) / 2) * 100,
                        "open_interest": c.get("open_interest")})
        return sorted(out, key=lambda r: (r["expiry"], r["option_type"], r["strike"]))

    def _options_tradeable_chain(self, underlying, exp_gte, exp_lte, around=None,
                                 width=10, max_spread_pct=None):
        cap = max_spread_pct or self.params.max_spread_pct_of_mid
        return [r for r in self._options_chain(underlying, exp_gte, exp_lte, around, width)
                if r["spread_pct"] <= cap]

    def _options_greeks(self, symbol, spot=None, iv=None, now=None):
        c = self._contracts.get(symbol)
        if not c:
            raise CapabilityError(f"{symbol} not in the session contract cache; "
                                  "call options.contracts or options.chain first")
        spot = spot or self._market_spot(c["underlying_symbol"])
        expiry = dt.datetime.fromisoformat(c["expiration_date"]).replace(hour=16, tzinfo=ET)
        t = bs.year_fraction(now or dt.datetime.now(dt.timezone.utc), expiry)
        if iv is None:
            q = self.rest.option_quotes([symbol]).get(symbol, {})
            mid = (float(q.get("bp", 0)) + float(q.get("ap", 0))) / 2
            iv = bs.implied_vol(mid, spot, float(c["strike_price"]), t, c["type"])
            if iv is None:
                raise CapabilityError(f"{symbol}: quote is unusable, no implied vol")
        g = bs.greeks(spot, float(c["strike_price"]), t, iv, c["type"])
        return {"iv": iv, "delta": g.delta, "gamma": g.gamma, "theta": g.theta,
                "vega": g.vega, "rho": g.rho, "t_years": t}

    def _options_payoff(self, legs, net_price, qty=1, points=40):
        return st.payoff_curve([_leg_from_dict(l) for l in legs], net_price, qty, points)

    def _options_enumerate(self, underlying, exp_gte, exp_lte, families=None,
                           widths=(1, 2, 3, 5, 10), width=10, max_spread_pct=None,
                           min_risk_reward=0.25, max_loss_cap=None, limit=60):
        """Deterministic structure search over the liquidity-gated chain."""
        chain = self._options_tradeable_chain(underlying, exp_gte, exp_lte,
                                              width=width, max_spread_pct=max_spread_pct)
        if not chain:
            return {"candidates": [], "note": "no tradeable contracts under the "
                                              "liquidity gate for that range"}
        spot = self._market_spot(underlying)
        fams = tuple(families) if families else cand.FAMILIES
        found = cand.enumerate_structures(chain, spot, underlying=underlying,
                                          families=fams, widths=tuple(widths))
        kept = cand.filter_candidates(found, min_risk_reward=min_risk_reward,
                                      max_loss_cap=max_loss_cap)
        for c in kept:
            self._candidates[c.id] = c
        shown = _diverse(kept, limit)
        return {"spot": round(spot, 2), "generated": len(found), "kept": len(kept),
                "families": sorted({c.family for c in kept}),
                "note": "ranked by round-trip crossing cost and sampled across "
                        "families; rank on edge with vol.evaluate, not on this order",
                "candidates": [c.to_json() for c in shown]}

    # ---- vol ---------------------------------------------------------------
    def _vol_realized(self, symbol, lookback=60, window=20):
        """Intraday series when warm, daily bars when not. Never silently None."""
        live = self.series.realized_vol(symbol, lookback)
        if live is not None:
            return {"value": round(live, 4), "source": "intraday"}
        bars = self._market_bars(symbol, "1Day")
        daily = vol.realized_from_bars(bars, window)
        ewma = vol.ewma_from_bars(bars)
        if daily is None:
            return {"value": None, "source": "unavailable"}
        return {"value": round(daily, 4), "ewma": round(ewma, 4) if ewma else None,
                "source": "daily_bars", "bars": len(bars)}

    def _vol_implied(self, price, spot, strike, t_years, option_type):
        return bs.implied_vol(price, spot, strike, t_years, option_type)

    def _vol_measures(self, symbol, days, sigma=None, skew=0.15):
        """Build the three real-world measures. Returns a handle plus a summary.

        Samples stay host-side: 60,000 floats have no business crossing the pipe.
        """
        spot = self._market_spot(symbol)
        if sigma is None:
            rv = self._vol_realized(symbol)
            sigma = rv.get("ewma") or rv.get("value")
        if not sigma:
            raise CapabilityError(f"{symbol}: no realized volatility available")
        bars = self._market_bars(symbol, "1Day")
        closes = [float(b["c"]) for b in bars if b.get("c")]
        built = ms.build(spot, float(sigma), float(days), closes, skew=skew)
        handle = f"m_{symbol}_{int(days)}_{len(self._measures)}"
        self._measures[handle] = built
        return {"handle": handle, "spot": round(spot, 2), "sigma": round(float(sigma), 4),
                "days": days,
                "measures": [{"name": m.name,
                              "p_up_1pct": round(m.prob(lambda s: s > spot * 1.01), 3),
                              "p_dn_1pct": round(m.prob(lambda s: s < spot * 0.99), 3),
                              "p_move_3pct": round(
                                  m.prob(lambda s: abs(s / spot - 1) > 0.03), 3)}
                             for m in built]}

    def _vol_evaluate(self, candidate_id, measure_handle):
        """Edge under every measure, and whether the candidate survives all of them."""
        c = self._candidates.get(candidate_id)
        if c is None:
            raise CapabilityError(f"unknown candidate {candidate_id!r}; call "
                                  "options.enumerate first")
        measures = self._measures.get(measure_handle)
        if measures is None:
            raise CapabilityError(f"unknown measure handle {measure_handle!r}; call "
                                  "vol.measures first")
        payoff = lambda spot: st.net_payoff_at(c.legs, spot)
        traded = c.net_price * 100.0
        out = ms.evaluate(payoff, measures, traded)
        out["candidate"] = candidate_id
        out["max_loss"] = round(c.max_loss, 2)
        out["risk_reward"] = round(c.risk_reward, 3)
        return out

    def _vol_rank(self, candidate_ids, measure_handle, top_k=3):
        """Rank stability across measures. A candidate that leads under only one
        convenient distribution is a modelling artifact."""
        measures = self._measures.get(measure_handle)
        if measures is None:
            raise CapabilityError(f"unknown measure handle {measure_handle!r}")
        rows = [{"id": cid} for cid in candidate_ids if cid in self._candidates]
        if not rows:
            raise CapabilityError("none of those candidate ids are known")
        payoff_of = lambda r: (lambda spot: st.net_payoff_at(
            self._candidates[r["id"]].legs, spot))
        price_of = lambda r: self._candidates[r["id"]].net_price * 100.0
        return ms.rank_stability(rows, measures, payoff_of, price_of, top_k=top_k)

    # ---- risk --------------------------------------------------------------
    def _risk_max_loss(self, legs, net_price, qty=1):
        return st.max_loss([_leg_from_dict(l) for l in legs], net_price, qty)

    def _risk_max_profit(self, legs, net_price, qty=1):
        v = st.max_profit([_leg_from_dict(l) for l in legs], net_price, qty)
        return None if v == st.UNBOUNDED else v

    def _risk_exposure(self):
        return {"open_positions": len(self.open_positions),
                "premium_at_risk": self.open_premium_at_risk,
                "realised_loss": self.realised_loss, "equity": self.equity}

    # ---- account -----------------------------------------------------------
    def _account_state(self):
        return {"equity": self.equity, "positions": self.open_positions,
                "realised_loss": self.realised_loss,
                "premium_at_risk": self.open_premium_at_risk}

    # ---- thesis ------------------------------------------------------------
    def _thesis_open(self, hypothesis, underlying, exit_profit, exit_invalidation,
                     exit_time, exit_news="", evidence_refs=None, gates=None):
        t = self.theses.open(hypothesis, underlying, exit_profit=exit_profit,
                             exit_invalidation=exit_invalidation, exit_time=exit_time,
                             exit_news=exit_news, evidence_refs=evidence_refs,
                             gates=gates)
        return t.to_json()

    def _thesis_list(self, status="open"):
        return [t.to_json() for t in self.theses.list(status)]

    def _thesis_history(self, limit=20):
        """Closed theses and how each ended. The bundle carries a digest; this is
        the full record when a cycle wants it."""
        return self.theses.outcomes(limit)

    def _thesis_close(self, thesis_id, reason, realised=None):
        return self.theses.close(thesis_id, reason=reason, realised=realised).to_json()

    def _thesis_note(self, thesis_id, note):
        return self.theses.update(thesis_id, note=note).to_json()

    # ---- trading (two-phase, gated) ---------------------------------------
    def _trading_execute(self, intent: dict):
        ti = _intent_from_dict(intent)
        kw = dict(equity=self.equity, open_premium_at_risk=self.open_premium_at_risk,
                  realised_loss=self.realised_loss, open_positions=self.open_positions)
        out = self.ex.execute(ti, **kw)
        if out.get("status") == "submitted":
            thesis = self.theses.get(ti.thesis_id)
            if thesis is not None:
                self.theses.update(ti.thesis_id, order_ids=[out["order_id"]])
        return out

    def _trading_preview(self, intent: dict):
        staged = self.ex.materialise(_intent_from_dict(intent), equity=self.equity,
                                     open_premium_at_risk=self.open_premium_at_risk,
                                     realised_loss=self.realised_loss,
                                     open_positions=self.open_positions, store=False)
        return {"qty": staged.verified.qty, "limit_price": staged.verified.limit_price,
                "max_loss": staged.verified.max_loss, "passed": staged.passed,
                "checklist": staged.checklist()}
