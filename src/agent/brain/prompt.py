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


@lru_cache(maxsize=8)
def _layer(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def system_prompt(*, include_pretrade: bool = False) -> str:
    """Put the response contract last so it has maximum instruction recency."""
    parts = [_layer("core"), _layer("domain")]
    if include_pretrade:
        parts.append(_layer("pretrade"))
    parts.append(OUTPUT_CONTRACT)
    return "\n\n---\n\n".join(parts)


def prompt_version(*, include_pretrade: bool = False) -> str:
    return hashlib.sha256(system_prompt(include_pretrade=include_pretrade).encode()
                          ).hexdigest()[:12]


def system_blocks(*, include_pretrade: bool = False) -> list[dict]:
    """System prompt as cacheable blocks.

    OpenAI-compatible providers cache the prefix themselves. Anthropic needs an
    explicit breakpoint, so the stable core+domain half carries one and the
    pre-trade layer -- which is only present on cycles that stage an order -- is a
    separate block. The output contract is always the final block.
    """
    stable = "\n\n---\n\n".join([_layer("core"), _layer("domain")])
    blocks = [{"type": "text", "text": stable,
               "cache_control": {"type": "ephemeral"}}]
    if include_pretrade:
        blocks.append({"type": "text", "text": _layer("pretrade"),
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


def repair_turn(traceback: str, hint: str) -> str:
    body = f"Your program failed.\n\n```\n{traceback[-2000:]}\n```"
    return body + (f"\n\nHint: {hint}" if hint else "") + \
        "\n\nFix it and continue from what you already computed. Do not restart.\n\n" + \
        OUTPUT_REMINDER


def observation_turn(stdout: str, staged_checklist: str | None = None) -> str:
    out = f"Program output:\n\n```\n{stdout[-4000:] or '(no output)'}\n```"
    if staged_checklist:
        out += ("\n\nAn order is staged and **not submitted**. Work the pre-trade "
                "checklist. In this new model program, call `trading.execute` once "
                "with the identical intent to confirm, or once with a corrected "
                "intent to revise.\n\n```\n"
                + staged_checklist + "\n```")
    return out + "\n\n" + OUTPUT_REMINDER
