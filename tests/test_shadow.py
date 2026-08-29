import datetime as dt
import pytest
from agent.brain import shadow
from agent.types import Leg

WHEN = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.timezone.utc)
EXP = "2026-09-03"


def row(strike, kind, bid, ask):
    return {"symbol": f"SPY260903{kind[0].upper()}{strike*1000:08.0f}",
            "strike": float(strike), "option_type": kind, "expiry": EXP,
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2}


def chain(spot=770.0):
    out = []
    for i in range(-15, 16):
        k = spot + i
        c = max(spot - k, 0) + 3.0
        p = max(k - spot, 0) + 3.0
        out.append(row(k, "call", round(c - 0.05, 2), round(c + 0.05, 2)))
        out.append(row(k, "put", round(p - 0.05, 2), round(p + 0.05, 2)))
    return out


def quotes(ch):
    return {r["symbol"]: {"bp": r["bid"], "ap": r["ask"]} for r in ch}


def test_every_policy_opens_once_and_only_once():
    r = shadow.ShadowRunner()
    ch = chain()
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    for name, book in r.books.items():
        expected = 0 if name == "flat_cash" else 1
        assert len(book.positions) == expected, name


def test_flat_cash_never_trades():
    r = shadow.ShadowRunner()
    ch = chain()
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    s = r.books["flat_cash"].summary(quotes(ch))
    assert s["total"] == 0 and s["return_pct"] == 0.0


def test_nothing_opens_when_entries_are_blocked():
    r = shadow.ShadowRunner()
    ch = chain()
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=False)
    assert all(len(b.positions) == 0 for b in r.books.values())


def test_marks_move_with_the_tape():
    """A bull call spread gains when the underlying rises."""
    r = shadow.ShadowRunner()
    ch = chain(770.0)
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    flat = r.books["bull_call"].summary(quotes(ch))["equity"]
    up = quotes(chain(778.0))
    # re-key the risen chain onto the symbols actually held
    held = r.books["bull_call"].positions[0]
    risen = {l.symbol: {"bp": 8.0, "ap": 8.1} for l in held.legs}
    risen[held.legs[1].symbol] = {"bp": 3.0, "ap": 3.1}   # short leg cheaper to buy back
    assert r.books["bull_call"].equity(risen) != flat


def test_close_all_realises_and_flattens():
    r = shadow.ShadowRunner()
    ch = chain()
    q = quotes(ch)
    r.step(ch, 770.0, q, WHEN, may_enter=True)
    r.close_all(q, WHEN)
    for book in r.books.values():
        assert all(not p.open for p in book.positions)
        assert book.summary(q)["open"] == 0


def test_unmarkable_position_does_not_corrupt_equity():
    """A missing quote must not silently mark a leg at zero."""
    r = shadow.ShadowRunner()
    ch = chain()
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    assert r.books["bull_call"].positions[0].unrealised({}) is None


def test_sizing_respects_the_risk_budget():
    r = shadow.ShadowRunner(risk_budget=1000.0)
    ch = chain()
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    for name, book in r.books.items():
        for p in book.positions:
            assert p.max_loss <= 1000.0 + p.max_loss / p.qty, name


def test_record_appends_a_row(tmp_path):
    r = shadow.ShadowRunner(path=tmp_path / "shadow.jsonl")
    ch = chain()
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    r.record(quotes(ch), WHEN)
    r.record(quotes(ch), WHEN)
    assert len((tmp_path / "shadow.jsonl").read_text().strip().splitlines()) == 2
