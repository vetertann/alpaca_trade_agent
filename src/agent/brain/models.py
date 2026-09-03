"""Provider and role configuration.

Roles name a provider, a model, and a fallback chain. A role whose key is absent
is skipped and its fallback used, with the substitution logged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent.config import get_key


@dataclass(frozen=True)
class ModelSpec:
    provider: str            # "anthropic" | "openai" | "nebius" | "featherless"
    model: str
    key_name: str
    base_url: str | None = None
    params: dict = field(default_factory=dict)
    request_timeout_s: float | None = None

    def available(self) -> bool:
        return bool(get_key(self.key_name, required=False))


NEBIUS = "https://api.studio.nebius.com/v1"
FEATHERLESS = "https://api.featherless.ai/v1"

# Measured 2026-08-29 against the real {thought, code} contract.
OPUS_5      = ModelSpec("anthropic", "claude-opus-5",   "ANTHROPIC_API_KEY",
                        params={"thinking": {"type": "adaptive",
                                             "display": "summarized"}})
OPUS_5_LOW  = ModelSpec("anthropic", "claude-opus-5",   "ANTHROPIC_API_KEY",
                        params={"thinking": {"type": "adaptive",
                                             "display": "summarized"},
                                "output_config": {"effort": "low"}})
SONNET_5    = ModelSpec("anthropic", "claude-sonnet-5", "ANTHROPIC_API_KEY",
                        params={"thinking": {"type": "adaptive",
                                             "display": "summarized"}})
HAIKU_45    = ModelSpec("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY")
GPT_56_SOL  = ModelSpec("openai", "gpt-5.6-sol",   "OPENAI_API_KEY",
                        params={"reasoning_effort": "medium"})
GPT_55      = ModelSpec("openai", "gpt-5.5",       "OPENAI_API_KEY")
GPT_54      = ModelSpec("openai", "gpt-5.4",       "OPENAI_API_KEY")
GPT_54_MINI = ModelSpec("openai", "gpt-5.4-mini",  "OPENAI_API_KEY")
KIMI_K3     = ModelSpec("nebius", "moonshotai/Kimi-K3",      "NEBIUS_API_KEY", NEBIUS,
                        request_timeout_s=150.0)
QWEN_397B   = ModelSpec("nebius", "Qwen/Qwen3.5-397B-A17B",  "NEBIUS_API_KEY", NEBIUS)

# zai-org/GLM-5.2 is deliberately absent from every chain: given an 8000-token
# budget on a code-generation prompt it consumed the whole allowance on reasoning
# and returned zero characters of content (measured 2026-08-29).

CHAINS: dict[str, list[ModelSpec]] = {
    # Production roles, used in the scored window. GPT-5.6 Sol is primary after
    # passing the real JSON/Python adapter contract locally. Opus and Kimi remain
    # heterogeneous fallbacks; Qwen is the final independent deployment path.
    "decision": [GPT_56_SOL, OPUS_5, KIMI_K3, QWEN_397B],
    "triage":   [QWEN_397B, HAIKU_45],
    "critic":   [GPT_54, SONNET_5],
    # Development roles: cheap Nebius models for build-and-test iteration, so
    # dry runs never spend frontier-model budget.
    "dev_decision": [KIMI_K3, QWEN_397B],
    "dev_triage":   [QWEN_397B],
}


def chain(role: str, *, dev: bool = False) -> list[ModelSpec]:
    """Every available model for a role, in preference order."""
    key = f"dev_{role}" if dev and f"dev_{role}" in CHAINS else role
    if key not in CHAINS:
        raise KeyError(f"unknown role {role!r}")
    out = [s for s in CHAINS[key] if s.available()]
    if not out:
        raise RuntimeError(f"no provider available for role {key!r}")
    return out


def resolve(role: str, *, dev: bool = False) -> ModelSpec:
    """First available model in the role's chain."""
    key = f"dev_{role}" if dev and f"dev_{role}" in CHAINS else role
    chain = CHAINS.get(key)
    if not chain:
        raise KeyError(f"unknown role {role!r}")
    for i, spec in enumerate(chain):
        if spec.available():
            if i:
                print(f"[models] {key}: falling back to {spec.provider}/{spec.model}")
            return spec
    raise RuntimeError(f"no provider available for role {key!r}")
