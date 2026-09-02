import datetime as dt
import json
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


def test_multi_expiry_chain_never_builds_a_calendar():
    near = chain_exp(770.0, "2026-09-01")
    far = chain_exp(770.0, "2026-09-03")
    mixed = near + far
    for name, policy in shadow.POLICIES.items():
        built = policy(mixed, 770.0)
        if name == "flat_cash":
            assert built is None
            continue
        legs, _, _ = built
        assert len({leg.expiry for leg in legs}) == 1, name


def test_bull_call_opens_at_normal_risk_on_a_multi_expiry_chain(tmp_path):
    ch = chain_exp(770.0, "2026-09-01") + chain_exp(770.0, "2026-09-03")
    r = shadow.ShadowRunner(path=tmp_path / "shadow.jsonl")
    r.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    pos = r.books["bull_call"].positions[0]
    assert pos.qty >= 1
    assert pos.max_loss <= r.risk_budget
    assert len({leg.expiry for leg in pos.legs}) == 1


def test_mark_uses_debit_positive_liquidation_values():
    legs = [Leg("LONG", 1, "buy", "buy_to_open", 770, "call",
                dt.date(2026, 9, 3)),
            Leg("SHORT", 1, "sell", "sell_to_open", 775, "call",
                dt.date(2026, 9, 3))]
    pos = shadow.ShadowPosition("bull_call", WHEN, legs, 1, 2.0, 200, "test")
    assert pos.mark({"LONG": {"bp": 4.0, "ap": 4.1},
                     "SHORT": {"bp": 1.0, "ap": 1.1}}) == pytest.approx(2.9)


def test_shadow_state_survives_restart_without_duplicate_entry(tmp_path):
    path = tmp_path / "shadow.jsonl"
    ch = chain_exp(770.0, "2026-09-03")
    first = shadow.ShadowRunner(path=path)
    first.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    first.record(quotes(ch), WHEN)

    restarted = shadow.ShadowRunner(path=path)
    original = first.books["bull_call"].positions[0]
    restored = restarted.books["bull_call"].positions[0]
    assert restored.legs == original.legs
    assert restored.entry_price == original.entry_price
    assert restarted.books["bull_call"].cash == first.books["bull_call"].cash
    restarted.step(ch, 770.0, quotes(ch), WHEN, may_enter=True)
    assert len(restarted.books["bull_call"].positions) == 1


def test_zero_dte_position_remains_live_until_1600_et(tmp_path):
    r = shadow.ShadowRunner(path=tmp_path / "shadow.jsonl")
    ch = chain_exp(770.0, "2026-09-01")
    before = dt.datetime(2026, 9, 1, 15, 59, tzinfo=shadow.ET)
    close = dt.datetime(2026, 9, 1, 16, 0, tzinfo=shadow.ET)
    r.step(ch, 770.0, quotes(ch), before, may_enter=True)
    pos = r.books["bull_call"].positions[0]
    assert not pos.expired(before)
    assert pos.expired(close)


# --- settlement and re-entry -------------------------------------------------

def chain_exp(spot=770.0, expiry="2026-09-01"):
    rows = chain(spot)
    return [{**r, "expiry": expiry,
             "symbol": r["symbol"].replace("260903", expiry.replace("-", "")[2:])}
            for r in rows]


def test_a_position_is_settled_at_expiry_not_dropped():
    """Once contracts expire their quotes vanish; an unsettled position would
    silently disappear from equity as if the premium had evaporated."""
    r = shadow.ShadowRunner()
    ch = chain_exp(770.0, "2026-09-01")
    monday = dt.datetime(2026, 8, 31, 15, 0, tzinfo=dt.timezone.utc)
    r.step(ch, 770.0, quotes(ch), monday, may_enter=True)
    book = r.books["bull_call"]
    assert book.positions and book.positions[0].open

    # the underlying rallies through the spread, then the contracts expire
    tuesday = dt.datetime(2026, 9, 1, 21, 0, tzinfo=dt.timezone.utc)
    r.settle_at_regular_closes({dt.date(2026, 9, 1): 790.0}, tuesday)
    p = book.positions[0]
    assert not p.open, "expired position must be settled"
    # a bull call spread finishing above both strikes settles at its full width
    assert book.realised > 0
    assert book.equity({}) > 100_000, "settled value must survive missing quotes"


def test_settlement_uses_intrinsic_value_not_a_quote():
    r = shadow.ShadowRunner()
    ch = chain_exp(770.0, "2026-09-01")
    r.step(ch, 770.0, quotes(ch), dt.datetime(2026, 8, 31, 15, tzinfo=dt.timezone.utc),
           may_enter=True)
    # expiring far below every strike: the spread is worthless, loss is the premium
    r.settle_at_regular_closes(
        {dt.date(2026, 9, 1): 700.0},
        dt.datetime(2026, 9, 1, 21, tzinfo=dt.timezone.utc))
    book = r.books["bull_call"]
    assert book.realised < 0
    assert book.equity({}) == pytest.approx(book.cash)


def test_a_book_re_enters_after_its_position_expires():
    """A baseline is the strategy run across the window, not one Monday trade."""
    r = shadow.ShadowRunner()
    mon_chain = chain_exp(770.0, "2026-09-01")
    r.step(mon_chain, 770.0, quotes(mon_chain),
           dt.datetime(2026, 8, 31, 15, tzinfo=dt.timezone.utc), may_enter=True)
    assert len(r.books["bull_call"].positions) == 1

    wed_chain = chain_exp(770.0, "2026-09-03")
    r.step(wed_chain, 770.0, quotes(wed_chain),
           dt.datetime(2026, 9, 2, 15, tzinfo=dt.timezone.utc), may_enter=True,
           settlement_spots={dt.date(2026, 9, 1): 770.0})
    assert len(r.books["bull_call"].positions) == 2, "should have opened a second"
    assert r.books["bull_call"].positions[1].open


def test_no_re_entry_while_a_position_is_still_live():
    r = shadow.ShadowRunner()
    ch = chain_exp(770.0, "2026-09-04")
    for hour in (15, 16, 17):
        r.step(ch, 770.0, quotes(ch),
               dt.datetime(2026, 9, 1, hour, tzinfo=dt.timezone.utc), may_enter=True)
    assert len(r.books["bull_call"].positions) == 1


def test_flat_cash_never_settles_anything():
    r = shadow.ShadowRunner()
    ch = chain_exp()
    r.step(ch, 770.0, quotes(ch), dt.datetime(2026, 8, 31, 15, tzinfo=dt.timezone.utc),
           may_enter=True)
    r.settle_at_regular_closes(
        {dt.date(2026, 9, 1): 800.0},
        dt.datetime(2026, 9, 2, 21, tzinfo=dt.timezone.utc))
    assert r.books["flat_cash"].equity({}) == 100_000.0


def test_expired_position_never_uses_a_later_live_spot():
    r = shadow.ShadowRunner()
    ch = chain_exp(770.0, "2026-09-01")
    r.step(ch, 770.0, quotes(ch),
           dt.datetime(2026, 8, 31, 15, tzinfo=dt.timezone.utc), may_enter=True)

    # The next morning gaps to 800.  Without the expiry-close mapping the
    # position must remain pending; 800 was never an executable expiry value.
    next_morning = dt.datetime(2026, 9, 2, 14, tzinfo=dt.timezone.utc)
    r.step([], 800.0, {}, next_morning, may_enter=False)
    assert r.books["long_straddle"].positions[0].open
    assert r.pending_expiries(next_morning) == {dt.date(2026, 9, 1)}


def test_settlement_records_canonical_close_and_provenance():
    r = shadow.ShadowRunner()
    ch = chain_exp(770.0, "2026-09-01")
    r.step(ch, 770.0, quotes(ch),
           dt.datetime(2026, 8, 31, 15, tzinfo=dt.timezone.utc), may_enter=True)
    later = dt.datetime(2026, 9, 2, 14, tzinfo=dt.timezone.utc)
    r.settle_at_regular_closes({dt.date(2026, 9, 1): 761.66}, later)

    pos = r.books["long_straddle"].positions[0]
    assert pos.closed_at == dt.datetime(2026, 9, 1, 16, 0, tzinfo=shadow.ET)
    assert pos.exit_source == "expiry_regular_close"


def test_schema_one_late_settlement_is_reopened_and_repaired(tmp_path):
    path = tmp_path / "shadow.jsonl"
    first = shadow.ShadowRunner(path=path)
    ch = chain_exp(770.0, "2026-09-01")
    first.step(ch, 770.0, quotes(ch),
               dt.datetime(2026, 8, 31, 15, tzinfo=dt.timezone.utc), may_enter=True)
    wrong_time = dt.datetime(2026, 9, 2, 9, 30, tzinfo=shadow.ET)
    for book in first.books.values():
        for pos in [p for p in book.positions if p.open]:
            book.settle_expired(pos, 800.0, wrong_time)
    first.record({}, wrong_time)

    state_path = path.with_suffix(".state.json")
    raw = json.loads(state_path.read_text())
    raw["schema_version"] = 1
    for book in raw["books"].values():
        for pos in book["positions"]:
            pos.pop("exit_source", None)
    state_path.write_text(json.dumps(raw))

    restored = shadow.ShadowRunner(path=path)
    pos = restored.books["long_straddle"].positions[0]
    assert pos.open
    assert restored.pending_expiries(wrong_time) == {dt.date(2026, 9, 1)}

    restored.settle_at_regular_closes(
        {dt.date(2026, 9, 1): 761.66}, wrong_time)
    assert not pos.open
    assert pos.exit_price == pytest.approx(abs(761.66 - 770.0))
    assert pos.exit_source == "expiry_regular_close"
