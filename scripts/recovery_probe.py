#!/usr/bin/env python
"""Development-account rehearsal for an accepted order whose response is lost.

This deliberately submits through the real Alpaca paper endpoint, discards the
successful response inside the client wrapper, raises TimeoutError, and requires the
durable executor to recover the order by client_order_id.  It is fault injection,
not evidence that Alpaca naturally timed out.

    PYTHONPATH=src .venv/bin/python scripts/recovery_probe.py --confirm
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from agent.config import ET, load_env, profile
from agent.host import gates
from agent.host.contracts import parse_occ_symbol
from agent.host.execution import Executor
from agent.host.ledger import ExecutionLedger
from agent.host.rest import Rest
from agent.host.risk_params import DEFAULT as RP
from agent.types import Leg, TradeIntent
from fill_probe import (cancel_if_live, flatten_probe_positions, pick_structure,
                        wait_filled)

OUT = Path(".run/recovery_probe.json")
TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected"}


class DropSuccessfulResponseOnce:
    """Delegate everything except the first successful order response."""
    def __init__(self, rest: Rest):
        self.rest = rest
        self.profile = rest.profile
        self.accepted: dict | None = None
        self.dropped = False

    def __getattr__(self, name):
        return getattr(self.rest, name)

    def submit_order_body(self, body: dict) -> dict:
        order = self.rest.submit_order_body(body)
        self.accepted = dict(order)
        if not self.dropped:
            self.dropped = True
            raise TimeoutError("fault injection: discarded successful broker response")
        return order


def trade_intent(api_legs: list[dict], risk_budget: float) -> TradeIntent:
    legs = []
    for raw in api_legs:
        meta = parse_occ_symbol(raw["symbol"])
        legs.append(Leg(meta.symbol, int(raw["ratio_qty"]), raw["side"],
                        raw["position_intent"], meta.strike, meta.option_type,
                        meta.expiry))
    return TradeIntent("SPY", "vertical_call", tuple(legs),
                       "th_dev_recovery_probe", risk_budget,
                       "fault-injected accepted-response recovery")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true",
                        help="required: submits and then flattens a real DEV order")
    args = parser.parse_args()

    load_env()
    prof = profile("dev")  # hard-coded: the competition profile is unreachable
    rest = Rest(prof, execution_transport="cli")
    now = dt.datetime.now(ET)
    account = rest.account()
    identity = gates.g_account_identity(account, "dev", prof.expected_account_id, now=now)
    if not identity.passed:
        raise PermissionError(identity.reason)
    if not rest.clock().get("is_open"):
        print("market closed; recovery probe requires live option quotes")
        return
    if not args.confirm:
        print("this fault-injects a lost response around a real development order; "
              "re-run with --confirm")
        return
    if rest.orders("open") or rest.positions():
        raise RuntimeError("development account must start flat with no open orders")

    api_legs, _, expiry = pick_structure(rest)
    symbols = {leg["symbol"] for leg in api_legs}
    wrapped = DropSuccessfulResponseOnce(rest)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    ledger = ExecutionLedger(f".run/recovery_probe-{stamp}.jsonl")
    executor = Executor(wrapped, RP, "dev", mode="execute", ledger=ledger,
                        expected_account_id=prof.expected_account_id)

    # Derive a one-structure risk budget from the host's own per-unit economics.
    provisional = trade_intent(api_legs, 10_000)
    preview = executor.materialise(
        provisional, equity=float(account["equity"]), now=dt.datetime.now(dt.timezone.utc),
        store=False)
    per_unit = preview.sizing.get("per_unit_max_loss")
    if not per_unit or per_unit <= 0:
        raise RuntimeError("could not derive positive per-unit risk for probe")
    intent = trade_intent(api_legs, float(per_unit) + 0.01)

    order_id = None
    evidence: dict = {"started_at": now.isoformat(), "expiry": expiry,
                      "classification": "fault-injected broker-backed rehearsal"}
    try:
        executor.begin_cycle("recovery-probe")
        executor.materialise(intent, equity=float(account["equity"]),
                             now=dt.datetime.now(dt.timezone.utc))
        ambiguous = executor.confirm(intent, equity=float(account["equity"]),
                                     now=dt.datetime.now(dt.timezone.utc))
        if ambiguous.get("status") != "unknown" or not wrapped.accepted:
            raise RuntimeError(f"fault injection did not produce UNKNOWN: {ambiguous}")
        coid = ambiguous["client_order_id"]
        order_id = str(wrapped.accepted["id"])
        print(f"accepted response discarded: broker={order_id} client={coid}")

        recovered = executor.reconcile_unresolved(now=dt.datetime.now(dt.timezone.utc))
        state = ledger.execution(coid)
        descriptor = ledger.descriptor_by_client_id(coid)
        if not state or state.get("status") != "submitted" or not descriptor:
            raise RuntimeError(f"exact-ID recovery failed: {recovered}")
        broker = rest.order_by_client_order_id(coid)
        print(f"recovered by client id: {broker.get('id')} status={broker.get('status')}")
        evidence |= {"ambiguous_result": ambiguous, "recovered": recovered,
                     "execution_state": state, "raw_broker_order": broker}

        final = wait_filled(rest, order_id, seconds=60)
        final = cancel_if_live(rest, order_id, final)
        evidence["entry_terminal"] = final
    finally:
        if order_id:
            try:
                current = rest.order(order_id)
                if str(current.get("status", "")).lower() not in TERMINAL:
                    rest.cancel(order_id)
            except Exception as exc:
                evidence["entry_cleanup_error"] = str(exc)
        try:
            closed = flatten_probe_positions(rest, symbols)
            if closed:
                evidence["close_terminal"] = closed
        except Exception as exc:
            evidence["position_cleanup_error"] = str(exc)
        evidence["positions_remaining"] = [
            p for p in rest.positions() if str(p.get("symbol")) in symbols]
        evidence["open_orders_remaining"] = rest.orders("open")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(evidence, indent=2, default=str))
        print(f"evidence written to {OUT}")
        if evidence["positions_remaining"] or evidence["open_orders_remaining"]:
            print("!! CLEANUP INCOMPLETE — flatten/cancel manually before any other test")


if __name__ == "__main__":
    main()
