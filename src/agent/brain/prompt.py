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


@lru_cache(maxsize=8)
def _layer(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def system_prompt(*, include_pretrade: bool = False) -> str:
    """Core + domain, plus the pre-trade layer only when an order is staged."""
    parts = [_layer("core"), _layer("domain")]
    if include_pretrade:
        parts.append(_layer("pretrade"))
    return "\n\n---\n\n".join(parts)


def prompt_version(*, include_pretrade: bool = False) -> str:
    return hashlib.sha256(system_prompt(include_pretrade=include_pretrade).encode()
                          ).hexdigest()[:12]


def system_blocks(*, include_pretrade: bool = False) -> list[dict]:
    """System prompt as cacheable blocks.

    OpenAI-compatible providers cache the prefix themselves. Anthropic needs an
    explicit breakpoint, so the stable core+domain half carries one and the
    pre-trade layer -- which is only present on cycles that stage an order -- is a
    separate block.
    """
    stable = "\n\n---\n\n".join([_layer("core"), _layer("domain")])
    blocks = [{"type": "text", "text": stable,
               "cache_control": {"type": "ephemeral"}}]
    if include_pretrade:
        blocks.append({"type": "text", "text": _layer("pretrade"),
                       "cache_control": {"type": "ephemeral"}})
    return blocks


def payload(bundle: dict) -> str:
    """The volatile half: the preflight bundle, and nothing else."""
    return ("Observation bundle for this cycle:\n\n```json\n"
            + json.dumps(bundle, indent=2, default=str)
            + "\n```\n\nWrite the program for this decision.")


def repair_turn(traceback: str, hint: str) -> str:
    body = f"Your program failed.\n\n```\n{traceback[-2000:]}\n```"
    return body + (f"\n\nHint: {hint}" if hint else "") + \
        "\n\nFix it and continue from what you already computed. Do not restart."


def observation_turn(stdout: str, staged_checklist: str | None = None) -> str:
    out = f"Program output:\n\n```\n{stdout[-4000:] or '(no output)'}\n```"
    if staged_checklist:
        out += ("\n\nAn order is staged and **not submitted**. Work the pre-trade "
                "checklist, then confirm with an identical intent or revise.\n\n```\n"
                + staged_checklist + "\n```")
    return out
