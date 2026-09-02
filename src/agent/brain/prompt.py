"""Prompt assembly.

Three layers, two of them hot-swappable. The stable layers are byte-identical
across cycles so provider-side caching applies to everything except the payload.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_ROBUST_RISK_PCT = 0.04
DEFAULT_SCENARIO_RISK_PCT = 0.04
DEFAULT_SINGLE_POSITION_RISK_PCT = 0.04
DEFAULT_TOTAL_PREMIUM_RISK_PCT = 0.15
DEFAULT_REALISED_LOSS_THROTTLE_PCT = 0.06
DEFAULT_ALIGNED_DIRECTION_RISK_PCT = 0.03
DEFAULT_BUILD_TARGET_RISK_PCT = 0.035
DEFAULT_SIZING_POSTURE = "balanced"

SIZING_POSTURE_GUIDANCE = {
    "balanced": "",
    "high_variance": (
        "**Runtime sizing posture: high-variance tournament.** For a genuinely "
        "different candidate that has three positive measures, stable rank, fresh "
        "edge after friction, and no conflicting directional evidence, aim to use a "
        "material share of the configured robust budget—normally 7–10% of equity "
        "maximum loss—rather than anchoring to the former 4% profile. Use less only "
        "when a named constraint or candidate-specific uncertainty justifies it. "
        "This is motivation to deploy strong evidence, not permission to promote "
        "weak evidence, duplicate a payoff, average down, or spend risk merely "
        "because headroom exists. A reasoned no-trade remains valid when no candidate "
        "qualifies."
    ),
}

OUTPUT_CONTRACT = """# Output contract

Every reply must be exactly one JSON object:

{
  "thought": "a concise plan; on confirmation turns include compact PASS/FAIL/N/A verdicts",
  "code": "valid executable Python source only"
}

Use exactly those two keys. Do not wrap the JSON in markdown fences. Do not add
text before or after the object."""

OUTPUT_REMINDER = (
    "Reply with exactly one JSON object containing only `thought` and `code`."
)


@lru_cache(maxsize=48)
def _layer(name: str, robust_risk_pct: float = DEFAULT_ROBUST_RISK_PCT,
           scenario_risk_pct: float = DEFAULT_SCENARIO_RISK_PCT,
           single_position_risk_pct: float = DEFAULT_SINGLE_POSITION_RISK_PCT,
           total_premium_risk_pct: float = DEFAULT_TOTAL_PREMIUM_RISK_PCT,
           realised_loss_throttle_pct: float = DEFAULT_REALISED_LOSS_THROTTLE_PCT,
           aligned_direction_risk_pct: float = DEFAULT_ALIGNED_DIRECTION_RISK_PCT,
           build_target_risk_pct: float = DEFAULT_BUILD_TARGET_RISK_PCT,
           sizing_posture: str = DEFAULT_SIZING_POSTURE) -> str:
    if not 0 < robust_risk_pct <= 0.15:
        raise ValueError("robust_risk_pct must be in (0, 0.15]")
    if not 0 < scenario_risk_pct <= 0.25:
        raise ValueError("scenario_risk_pct must be in (0, 0.25]")
    for label, value, limit in (
            ("single_position_risk_pct", single_position_risk_pct, 0.25),
            ("total_premium_risk_pct", total_premium_risk_pct, 0.60),
            ("realised_loss_throttle_pct", realised_loss_throttle_pct, 0.25),
            ("aligned_direction_risk_pct", aligned_direction_risk_pct, 0.15),
            ("build_target_risk_pct", build_target_risk_pct, 0.25)):
        if not 0 < value <= limit:
            raise ValueError(f"{label} must be in (0, {limit}]")
    if robust_risk_pct > single_position_risk_pct:
        raise ValueError("robust_risk_pct cannot exceed single_position_risk_pct")
    if aligned_direction_risk_pct > single_position_risk_pct:
        raise ValueError(
            "aligned_direction_risk_pct cannot exceed single_position_risk_pct")
    if single_position_risk_pct > total_premium_risk_pct:
        raise ValueError(
            "single_position_risk_pct cannot exceed total_premium_risk_pct")
    if build_target_risk_pct > scenario_risk_pct:
        raise ValueError("build_target_risk_pct cannot exceed scenario_risk_pct")
    if sizing_posture not in SIZING_POSTURE_GUIDANCE:
        raise ValueError(f"unknown sizing_posture: {sizing_posture}")
    percent = f"{robust_risk_pct * 100:g}%"
    fraction = f"{robust_risk_pct:g}"
    scenario_percent = f"{scenario_risk_pct * 100:g}%"
    return ((PROMPTS / f"{name}.md").read_text()
            .replace("{{ROBUST_RISK_PERCENT}}", percent)
            .replace("{{ROBUST_RISK_FRACTION}}", fraction)
            .replace("{{SCENARIO_RISK_PERCENT}}", scenario_percent)
            .replace("{{SINGLE_POSITION_RISK_PERCENT}}",
                     f"{single_position_risk_pct * 100:g}%")
            .replace("{{TOTAL_PREMIUM_RISK_PERCENT}}",
                     f"{total_premium_risk_pct * 100:g}%")
            .replace("{{REALISED_LOSS_THROTTLE_PERCENT}}",
                     f"{realised_loss_throttle_pct * 100:g}%")
            .replace("{{ALIGNED_DIRECTION_RISK_PERCENT}}",
                     f"{aligned_direction_risk_pct * 100:g}%")
            .replace("{{ALIGNED_DIRECTION_RISK_FRACTION}}",
                     f"{aligned_direction_risk_pct:g}")
            .replace("{{BUILD_TARGET_RISK_PERCENT}}",
                     f"{build_target_risk_pct * 100:g}%")
            .replace("{{SIZING_POSTURE_GUIDANCE}}",
                     SIZING_POSTURE_GUIDANCE[sizing_posture]))


def system_prompt(*, include_pretrade: bool = False,
                  robust_risk_pct: float = DEFAULT_ROBUST_RISK_PCT,
                  scenario_risk_pct: float = DEFAULT_SCENARIO_RISK_PCT,
                  **risk_profile) -> str:
    """Put the response contract last so it has maximum instruction recency."""
    parts = [_layer("core", robust_risk_pct, scenario_risk_pct, **risk_profile),
             _layer("domain", robust_risk_pct, scenario_risk_pct, **risk_profile)]
    if include_pretrade:
        parts.append(_layer("pretrade", robust_risk_pct, scenario_risk_pct,
                            **risk_profile))
    parts.append(OUTPUT_CONTRACT)
    return "\n\n---\n\n".join(parts)


def prompt_version(*, include_pretrade: bool = False,
                   robust_risk_pct: float = DEFAULT_ROBUST_RISK_PCT,
                   scenario_risk_pct: float = DEFAULT_SCENARIO_RISK_PCT,
                   **risk_profile) -> str:
    return hashlib.sha256(system_prompt(include_pretrade=include_pretrade,
                                        robust_risk_pct=robust_risk_pct,
                                        scenario_risk_pct=scenario_risk_pct,
                                        **risk_profile).encode()
                          ).hexdigest()[:12]


def system_blocks(*, include_pretrade: bool = False,
                  robust_risk_pct: float = DEFAULT_ROBUST_RISK_PCT,
                  scenario_risk_pct: float = DEFAULT_SCENARIO_RISK_PCT,
                  **risk_profile) -> list[dict]:
    """System prompt as cacheable blocks.

    OpenAI-compatible providers cache the prefix themselves. Anthropic needs an
    explicit breakpoint, so the stable core+domain half carries one and the
    pre-trade layer -- which is only present on cycles that stage an order -- is a
    separate block. The output contract is always the final block.
    """
    stable = "\n\n---\n\n".join([
        _layer("core", robust_risk_pct, scenario_risk_pct, **risk_profile),
        _layer("domain", robust_risk_pct, scenario_risk_pct, **risk_profile)])
    blocks = [{"type": "text", "text": stable,
               "cache_control": {"type": "ephemeral"}}]
    if include_pretrade:
        blocks.append({"type": "text", "text": _layer(
            "pretrade", robust_risk_pct, scenario_risk_pct, **risk_profile),
                       "cache_control": {"type": "ephemeral"}})
    blocks.append({"type": "text", "text": OUTPUT_CONTRACT})
    return blocks


def payload(bundle: dict) -> str:
    """The volatile half: the preflight bundle, and nothing else."""
    return ("Observation bundle for this cycle:\n\n```json\n"
            + json.dumps(bundle, indent=2, default=str)
            + "\n```\n\nWrite the program for this decision.")


def repeat_turn(traceback: str, hint: str, failing_line: str = "") -> str:
    """The model returned the identical program after being shown the failure.

    Restating the same traceback invites the same output again, so name the repeat
    and demand the specific edit instead.
    """
    body = ("You returned a byte-identical program. It will fail in exactly the same "
            "way, so running it again would waste the round.\n\n"
            f"The failure was:\n\n```\n{traceback[-1200:]}\n```")
    if failing_line:
        body += f"\n\nThe offending line is:\n\n    {failing_line}"
    if hint:
        body += f"\n\n{hint}"
    return body + ("\n\nChange that specific line. Do not re-send the previous "
                   "program.\n\nReply with exactly one JSON object containing only "
                   "`thought` and `code`.")


def _last_round_guidance(rounds_remaining: int | None) -> str:
    if rounds_remaining != 1:
        return ""
    return (
        "\n\nExactly one program round remains. There is no later program available "
        "to confirm a new `trading.execute` or `trading.execute_if` draft. If entry "
        "evidence is complete, finish now with `trading.set_entry_trigger(...)` and "
        "an explicit price boundary; arming that host-watched trigger is the market "
        "action for this cycle. Otherwise finish with `decision.no_trade(reason)`. "
        "Do not merely stage a new order."
    )


def repair_turn(traceback: str, hint: str, source: str = "",
                rounds_remaining: int | None = None) -> str:
    body = f"Your program failed.\n\n```\n{traceback[-2000:]}\n```"
    if source:
        body += f"\n\nThe program that failed was:\n\n```python\n{source[-8000:]}\n```"
    return body + (f"\n\nHint: {hint}" if hint else "") + \
        _last_round_guidance(rounds_remaining) + \
        "\n\nFix it and continue from what you already computed. Do not restart.\n\n" + \
        OUTPUT_REMINDER


def observation_turn(stdout: str, staged_checklist: str | None = None) -> str:
    out = f"Program output:\n\n```\n{stdout[-4000:] or '(no output)'}\n```"
    if staged_checklist:
        out += ("\n\nAn order is staged and **not submitted**. Work the pre-trade "
                "checklist. In this new model program, use the host-supplied exact "
                "confirmation call with the identical intent. A conditional "
                "`trading.execute_if` draft cannot be confirmed by switching to "
                "`trading.execute`. Or revise once with a corrected intent.\n\n```\n"
                + staged_checklist + "\n```")
    return out + "\n\n" + OUTPUT_REMINDER


def review_turn(bundle: dict, staged: dict, evidence: dict,
                staged_checklist: str, thesis: dict | None = None) -> str:
    """Build a clean confirmation request without proposal thought or source."""
    packet = {"observation": bundle, "canonical_staged_order": staged,
              "host_recorded_evidence": evidence,
              "canonical_thesis": thesis}
    return ("Review this staged order from a clean context. It is not submitted. "
            "The proposal's prior "
            "thought and program are deliberately omitted. Treat only this host "
            "packet and checklist as authoritative. The `confirmation_call` field "
            "is exact and machine-readable: call that function once with the "
            "identical persisted intent and every listed kwarg. In particular, "
            "never switch a staged `execute_if` draft to `trading.execute`. "
            "Otherwise revise it or decline it.\n\n```json\n"
            + json.dumps(packet, indent=2, default=str)
            + "\n```\n\nHost checklist:\n\n```\n" + staged_checklist
            + "\n```\n\n" + OUTPUT_REMINDER)


def missing_evidence_turn(bundle: dict, result: dict, rounds_remaining: int) -> str:
    """Repair a procedural omission without retaining proposal reasoning."""
    return (payload(bundle) + "\n\nThe host did not stage or submit the proposed "
            "entry because required evidence was missing:\n\n```json\n"
            + json.dumps(result, indent=2, default=str)
            + "\n```\n\nCall the missing capabilities for the exact candidate, reconsider "
            "their returned results, and then execute again or decline. "
            f"{rounds_remaining} program round(s) remain."
            + _last_round_guidance(rounds_remaining)
            + "\n\n" + OUTPUT_REMINDER)


def revision_turn(bundle: dict, result: dict, rounds_remaining: int) -> str:
    """Return categorical candidate/intent/thesis mismatches without persuasion."""
    return (payload(bundle) + "\n\nThe host did not stage or submit the entry "
            "because the candidate, intent, or recorded thesis violates a "
            "categorical policy:\n\n"
            "```json\n" + json.dumps(result, indent=2, default=str)
            + "\n```\n\nSelect an aligned structure, correct the risk budget or "
            "thesis as named, and execute the corrected intent once—or decline. "
            f"{rounds_remaining} program round(s) remain."
            + _last_round_guidance(rounds_remaining)
            + "\n\n" + OUTPUT_REMINDER)


def continuation_turn(stdout: str, rounds_remaining: int) -> str:
    """Return every successful nonterminal program result to the model."""
    out = f"Program output:\n\n```\n{stdout[-4000:] or '(no output)'}\n```"
    out += ("\n\nThis program completed without a terminal submission. Use the new "
            "evidence above and the variables already persisted by the runtime. "
            "Do not repeat the same simulation or data fetch. "
            f"{rounds_remaining} program round(s) remain, including any separate "
            "order-confirmation program. Finish with `decision.no_trade(reason)`, "
            "or stage/confirm an order with `trading.execute_if` and an explicit "
            "fresh-price boundary in execute mode. Repeat the exact same conditional "
            "call in the later confirmation program. If neither "
            "terminal action is taken, the next successful program result is "
            "returned again while budget remains.")
    return (out + _last_round_guidance(rounds_remaining)
            + "\n\n" + OUTPUT_REMINDER)


def state_turn(manifest: dict | None) -> str:
    """Latest-only runtime state, injected ephemerally rather than accumulated."""
    manifest = manifest or {}
    persisted = manifest.get("persisted") or []
    dropped = manifest.get("dropped") or []
    lines = ["# Current persisted program state (authoritative)", ""]
    if persisted:
        lines.extend(
            f"- `{item['name']}`: {item.get('type', 'unknown')}"
            for item in persisted if item.get("name"))
    else:
        lines.append("- (none)")
    lines += ["", "Only the names above remain available from earlier programs. "
              "Preloaded `obs`, modules, and capability namespaces remain available "
              "regardless and are intentionally omitted."]
    if dropped:
        lines += ["", "Dropped after the previous program:"]
        lines.extend(
            f"- `{item['name']}`: {item.get('type', 'unknown')} — "
            f"{item.get('reason', 'not persisted')}"
            for item in dropped if item.get("name"))
        lines += ["", "Recreate a dropped value only if the current decision still "
                  "needs it; prefer the printed summary when it is sufficient."]
    return "\n".join(lines)
