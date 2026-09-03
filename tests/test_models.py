import pytest
from agent.brain import models


def test_glm_is_excluded_from_every_chain():
    """It consumed an 8000-token budget on reasoning and returned no content."""
    named = {s.model for chain in models.CHAINS.values() for s in chain}
    assert not any("GLM" in m for m in named)


def test_gpt_oss_120b_is_excluded_and_qwen_is_the_nebius_fallback():
    named = {s.model for chain in models.CHAINS.values() for s in chain}
    assert "openai/gpt-oss-120b" not in named
    assert models.CHAINS["decision"][-1].model == "Qwen/Qwen3.5-397B-A17B"
    assert models.CHAINS["triage"][0].model == "Qwen/Qwen3.5-397B-A17B"


def test_dev_chains_are_cheap_nebius_models():
    assert all(s.provider == "nebius" for s in models.CHAINS["dev_decision"])


def test_resolve_prefers_the_head_of_the_chain(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert models.resolve("decision").model == "claude-opus-5"


def test_gpt_56_sol_is_primary_when_openai_is_available(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    resolved = models.resolve("decision")
    assert resolved.provider == "openai"
    assert resolved.model == "gpt-5.6-sol"
    assert resolved.params["reasoning_effort"] == "medium"


def test_resolve_falls_back_when_a_key_is_missing(monkeypatch):
    """Only OpenAI is reachable, so the chain must walk past Anthropic and Nebius."""
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert models.resolve("decision").provider == "openai"


def test_resolve_walks_the_chain_in_order(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "x")
    assert models.resolve("decision").model == "moonshotai/Kimi-K3"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert models.resolve("decision").model == "claude-opus-5"


def test_resolve_raises_when_nothing_is_available():
    with pytest.raises(RuntimeError, match="no provider available"):
        models.resolve("decision")


def test_unknown_role_raises():
    with pytest.raises(KeyError):
        models.resolve("nonsense")


def test_dev_flag_selects_the_dev_chain(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "x")
    assert models.resolve("decision", dev=True).model == "moonshotai/Kimi-K3"
