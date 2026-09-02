import json
import pytest
from agent.brain.models import ModelSpec
from agent.brain.providers import ChainProvider, Provider
from agent.brain.providers import ContractError, parse_contract


def test_plain_object():
    t, c = parse_contract('{"thought": "plan", "code": "print(1)"}')
    assert t == "plan" and c == "print(1)"


def test_fenced_object():
    t, c = parse_contract('```json\n{"thought": "p", "code": "x=1"}\n```')
    assert c == "x=1"


def test_object_with_surrounding_prose():
    t, c = parse_contract('Sure!\n{"thought": "p", "code": "x=1"}\nHope that helps.')
    assert c == "x=1"


def test_empty_reply_names_the_cause():
    with pytest.raises(ContractError, match="budget was consumed"):
        parse_contract("")


def test_missing_key_is_named():
    with pytest.raises(ContractError, match="thought"):
        parse_contract('{"code": "x=1"}')


def test_code_must_be_a_string():
    with pytest.raises(ContractError, match="must be a string"):
        parse_contract('{"thought": "p", "code": ["x=1"]}')


def test_non_json_reports_the_prefix():
    with pytest.raises(ContractError, match="not JSON"):
        parse_contract("I cannot help with that.")


def test_truncated_object_is_a_contract_error_not_a_json_error():
    """A cut-off reply must reach the repair loop, not escape as JSONDecodeError."""
    with pytest.raises(ContractError, match="cut off"):
        parse_contract('{"thought": "plan", "code": "print(\'hello')


def test_trailing_prose_is_recovered_not_rejected():
    t, c = parse_contract('{"thought": "a", "code": "x=1"} hope that helps')
    assert c == "x=1"


def test_malformed_object_explains_the_likely_cause():
    with pytest.raises(ContractError, match="malformed"):
        parse_contract('{"thought": "a", "code": x=1}')


# --- structural validation ---------------------------------------------------

def test_code_must_be_valid_python():
    """Unparseable source is a structural failure, not something to run and see."""
    with pytest.raises(ContractError, match="not valid Python"):
        parse_contract('{"thought": "p", "code": "def broken(:\\n  pass"}')


def test_empty_code_is_refused():
    with pytest.raises(ContractError, match="empty"):
        parse_contract('{"thought": "p", "code": "   "}')


def test_valid_multiline_program_passes():
    src = "spot = market.spot('SPY')\nprint(spot)\n"
    t, c = parse_contract(json.dumps({"thought": "p", "code": src}))
    assert c == src


# --- terminal provider errors ------------------------------------------------

def test_rate_limit_is_terminal():
    from agent.brain.providers import is_terminal
    assert is_terminal(RuntimeError("429 rate_limit_error: too many requests"))
    assert is_terminal(RuntimeError("Error code: 529 - overloaded"))
    assert is_terminal(RuntimeError("insufficient_quota"))


def test_ordinary_failures_are_not_terminal():
    from agent.brain.providers import is_terminal
    assert not is_terminal(ValueError("connection reset"))
    assert not is_terminal(TimeoutError("provider exceeded 70s"))


def test_repair_attempt_usage_is_aggregated(monkeypatch):
    provider = Provider(ModelSpec("test", "model", "TEST_KEY"))
    replies = iter([
        ("not json", {"input_tokens": 100, "output_tokens": 20,
                      "cached_tokens": 5, "cache_write_tokens": 0,
                      "reasoning": "", "reasoning_tokens": 3}),
        ('{"thought":"ok","code":"print(1)"}',
         {"input_tokens": 120, "output_tokens": 30,
          "cached_tokens": 7, "cache_write_tokens": 2,
          "reasoning": "final", "reasoning_tokens": 4}),
    ])
    monkeypatch.setattr(provider, "_openai_compatible",
                        lambda system, messages: next(replies))

    completion = provider.complete("system", [], repairs=1)

    assert completion.attempts == 2
    assert completion.input_tokens == 220
    assert completion.output_tokens == 50
    assert completion.cached_tokens == 12
    assert completion.cache_write_tokens == 2
    assert completion.reasoning_tokens == 7
    assert completion.reasoning == "final"


def test_chain_passes_request_timeout_to_each_provider():
    chain = ChainProvider(
        [ModelSpec("test", "a", "A"), ModelSpec("test", "b", "B")],
        timeout_s=17.0)
    assert [provider.request_timeout_s for provider in chain.providers] == [17.0, 17.0]


def test_model_specific_timeout_overrides_chain_default():
    chain = ChainProvider(
        [ModelSpec("nebius", "slow", "KEY", request_timeout_s=150.0),
         ModelSpec("openai", "normal", "KEY")],
        timeout_s=70.0)
    assert [provider.request_timeout_s for provider in chain.providers] == [150.0, 70.0]
