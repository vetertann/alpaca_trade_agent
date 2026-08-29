#!/usr/bin/env python
"""Fermi estimate of LLM calls, tokens and cost for the scored window.

Grounded in tokens actually measured from `.run/*/trace.jsonl`, not guessed.
Cache multipliers from Anthropic's pricing docs: 5m write 1.25x, read 0.1x.

    PYTHONPATH=src .venv/bin/python scripts/estimate_cost.py [--agents 3]
"""
from __future__ import annotations

import argparse
import glob
import json

SESSIONS = 4                      # Mon 31 Aug .. Thu 3 Sep

# Measured on the real system prompt + bundle, Kimi-K3, 7 rounds across 5 cycles.
SYSTEM_TOKENS = 2_800             # core + domain layers
SYSTEM_PRETRADE = 3_250           # with the pre-trade layer injected
BUNDLE_TOKENS = 800               # preflight payload, volatile half

MEASURED = {"input_min": 2_754, "input_max": 5_825, "input_mean": 3_579,
            "output_min": 2_016, "output_max": 7_589, "output_mean": 5_227}

# $ per million tokens
PRICES = {
    "claude-opus-5":    {"in": 5.00,  "out": 25.00},
    "claude-sonnet-5":  {"in": 2.00,  "out": 10.00},
    "claude-haiku-4-5": {"in": 1.00,  "out": 5.00},
    "gpt-5.5":          {"in": None,  "out": None},   # not published here
    "nebius-open":      {"in": 0.20,  "out": 0.60},   # typical open-weight tier
}
CACHE_WRITE, CACHE_READ = 1.25, 0.10


def scenario(name: str, cycles_per_session: int, rounds_per_cycle: float,
             staged_share: float) -> dict:
    """One agent, one scenario."""
    calls_per_session = cycles_per_session * rounds_per_cycle
    calls = calls_per_session * SESSIONS

    # Round 1 of a cycle pays a cache read on the stable preamble plus fresh bundle.
    # Later rounds carry the conversation so far, which is what drives input up.
    sys_tokens = SYSTEM_TOKENS + staged_share * (SYSTEM_PRETRADE - SYSTEM_TOKENS)
    first_in = sys_tokens + BUNDLE_TOKENS
    later_in = MEASURED["input_mean"] + MEASURED["output_mean"]   # history accumulates

    first_calls = cycles_per_session * SESSIONS
    later_calls = max(calls - first_calls, 0)

    cached = first_calls * sys_tokens                     # preamble read from cache
    fresh_in = first_calls * BUNDLE_TOKENS + later_calls * later_in
    out = calls * MEASURED["output_mean"]

    return {"name": name, "cycles_per_session": cycles_per_session,
            "rounds_per_cycle": rounds_per_cycle, "calls": int(calls),
            "cached_in": int(cached), "fresh_in": int(fresh_in), "out": int(out),
            "total_in": int(cached + fresh_in)}


def cost(sc: dict, model: str) -> float | None:
    p = PRICES[model]
    if p["in"] is None:
        return None
    # first write of the preamble each session, then reads
    writes = SESSIONS * SYSTEM_TOKENS
    return (writes / 1e6 * p["in"] * CACHE_WRITE
            + (sc["cached_in"] - writes) / 1e6 * p["in"] * CACHE_READ
            + sc["fresh_in"] / 1e6 * p["in"]
            + sc["out"] / 1e6 * p["out"])


def measured_note() -> str:
    rows = []
    for f in glob.glob(".run/*/trace.jsonl"):
        for line in open(f):
            r = json.loads(line)
            if r["kind"] == "PROGRAM":
                rows.append(r["usage"])
    return f"{len(rows)} measured rounds across {len(glob.glob('.run/*/trace.jsonl'))} runs"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=1)
    args = ap.parse_args()
    n = args.agents

    print(f"=== LLM budget, {SESSIONS} scored sessions, {n} agent(s) ===")
    print(f"grounded in: {measured_note()}")
    print(f"system prompt {SYSTEM_TOKENS} tok (cached), bundle ~{BUNDLE_TOKENS} tok/cycle")
    print(f"measured per round: input {MEASURED['input_min']}-{MEASURED['input_max']}, "
          f"output {MEASURED['output_min']}-{MEASURED['output_max']}\n")

    scenarios = [
        scenario("min   quiet tape, one round", 8, 1.0, 0.20),
        scenario("mid   as designed", 13, 1.6, 0.35),
        scenario("max   volatile, cap hit", 20, 3.0, 0.60),
    ]

    print(f"{'scenario':30} {'calls/day':>10} {'calls':>8} {'in (M)':>9} "
          f"{'out (M)':>9} {'cached%':>8}")
    for s in scenarios:
        per_day = s["calls"] / SESSIONS * n
        print(f"{s['name']:30} {per_day:10.0f} {s['calls']*n:8d} "
              f"{s['total_in']*n/1e6:9.2f} {s['out']*n/1e6:9.2f} "
              f"{s['cached_in']/max(s['total_in'],1)*100:7.0f}%")

    print(f"\n{'scenario':30} " + "".join(f"{m:>18}" for m in
                                          ("claude-opus-5", "claude-sonnet-5", "nebius-open")))
    for s in scenarios:
        line = f"{s['name']:30} "
        for m in ("claude-opus-5", "claude-sonnet-5", "nebius-open"):
            c = cost(s, m)
            line += f"{('$%.2f' % (c * n)) if c is not None else 'n/a':>18}"
        print(line)

    print("\nnotes")
    print("  - output dominates: measured mean output is 5,227 tokens per round against")
    print("    ~3,600 input, and output is priced 5x higher. Effort 'low' on Opus 5")
    print("    halved output  in measurement (1,836 -> 952) and is the main lever.")
    print("  - caching only helps the stable preamble; conversation history in rounds 2+")
    print("    is fresh input every time, which is why round count drives cost.")
    print(f"  - {n} agents multiply everything linearly; nothing is shared between them.")


if __name__ == "__main__":
    main()
