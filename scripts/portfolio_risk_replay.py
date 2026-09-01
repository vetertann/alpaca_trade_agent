#!/usr/bin/env python3
"""Capture immutable trade-print evidence and replay Monday admissions."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

from agent.config import load_env, profile
from agent.host.rest import Rest
from agent.host.risk_replay import load_events, replay_sample


def _parse_sample(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError("sample must be NAME=PATH")
    return name, Path(raw_path)


def _iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _capture(rest: Rest, directory: Path, sample: str) -> None:
    events = load_events(directory, sample)
    payload = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "Alpaca historical trade endpoints; historical option quotes unavailable",
        "events": {},
    }
    for event in events:
        filled = _iso(event["filled_at"])
        submitted = _iso(event["submitted_at"])
        cluster = ["SPY", "QQQ", "IWM"]
        stock_start = (filled - dt.timedelta(seconds=1)).isoformat()
        stock_end = (filled + dt.timedelta(seconds=1)).isoformat()
        option_start = (submitted - dt.timedelta(minutes=10)).isoformat()
        option_end = (filled + dt.timedelta(seconds=1)).isoformat()
        symbols = [str(leg["symbol"]) for leg in event["legs"]]
        payload["events"][event["client_order_id"]] = {
            "filled_at": event["filled_at"], "submitted_at": event["submitted_at"],
            "stock_window": {"start": stock_start, "end": stock_end},
            "option_window": {"start": option_start, "end": option_end},
            "stock_trades": rest.stock_trades(cluster, stock_start, stock_end),
            "option_trades": rest.option_trades(symbols, option_start, option_end),
        }
    destination = directory / "market_trades.json"
    destination.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"{sample}: captured {len(events)} filled events -> {destination} sha256={digest}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="append", type=_parse_sample, required=True,
                        help="independent account sample as NAME=PATH")
    parser.add_argument("--capture-market", action="store_true")
    parser.add_argument("--profile", default="competition")
    parser.add_argument("--output", type=Path,
                        default=Path(".run/portfolio-risk-calibration.json"))
    args = parser.parse_args()
    if args.capture_market:
        load_env()
        rest = Rest(profile(args.profile))
        try:
            for name, directory in args.sample:
                _capture(rest, directory, name)
        finally:
            rest.close()
    thresholds = [round(0.50 + 0.05 * index, 2) for index in range(31)]
    samples = [replay_sample(directory, name, thresholds=thresholds)
               for name, directory in args.sample]
    by_name = {sample["sample"]: sample for sample in samples}
    primary = by_name.get("competition")
    holdout = by_name.get("dev")
    selected = None
    if primary and holdout and primary["events"]:
        first = primary["events"][0]
        for threshold in thresholds:
            key = round(float(threshold), 4)
            primary_first = next(cell for cell in first["threshold_grid"]
                                 if cell["threshold_pct"] == key)
            holdout_summary = next(row for row in holdout["threshold_summary"]
                                   if row["threshold_pct"] == key)
            if (primary_first["decision"] == "pass"
                    and holdout_summary["passed_entries"] == holdout["entry_count"]):
                selected = key
                break
    sensitivity_rows = [
        leg for sample in samples for event in sample["events"]
        for leg in event["per_leg_iv"].values()
        if leg.get("iv_difference") is not None]
    differences = sorted(abs(float(row["iv_difference"]))
                         for row in sensitivity_rows)
    sensitivity = {
        "comparable_legs": len(differences),
        "median_absolute_iv_difference": (
            differences[len(differences) // 2] if differences else None),
        "maximum_absolute_iv_difference": max(differences) if differences else None,
    }
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "thresholds_pct": thresholds, "samples": samples,
        "selection": ({
            "max_correlated_scenario_loss_pct": selected,
            "criterion": (
                "smallest grid value that admits the primary account's first "
                "standalone structure at observed size and every holdout entry on "
                "its independent chronological policy path"),
            "pnl_not_used": True,
        } if selected is not None else None),
        "fill_vs_prior_trade_iv_sensitivity": sensitivity,
        "selection_rule": (
            "account-level paths are compared separately; fills share one market "
            "regime and are not treated as independent observations"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.output}")
    for sample in samples:
        required = [row["required_loss_limit_pct"] for row in sample["events"]]
        print(f"{sample['sample']}: {sample['entry_count']} entries; "
              f"required limits {', '.join(f'{value:.3f}%' for value in required)}")
    print(f"selected correlated scenario limit: {selected}%")


if __name__ == "__main__":
    main()
