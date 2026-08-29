"""The chain must fall through on every way a provider can stop answering."""
import pytest
from agent.brain.models import ModelSpec
from agent.brain.providers import ChainProvider, Completion


def spec(name):
    return ModelSpec("test", name, "NEBIUS_API_KEY")


class Fake:
    """Stands in for Provider."""
    def __init__(self, name, behaviour):
        self.spec = spec(name)
        self.behaviour = behaviour
        self.calls = 0

    def complete(self, system, messages, *, repairs=3):
        self.calls += 1
        b = self.behaviour
        if isinstance(b, Exception):
            raise b
        if b == "malformed":
            return Completion("", "", "not json", "test", self.spec.model, 0.1,
                              error="reply is not JSON")
        return Completion("plan", "print(1)", "raw", "test", self.spec.model, 0.1)


def chain(*fakes, timeout=0.0):
    c = ChainProvider([f.spec for f in fakes], timeout_s=timeout)
    c.providers = list(fakes)
    return c


def test_first_provider_answers():
    a, b = Fake("opus", "ok"), Fake("kimi", "ok")
    out = chain(a, b).complete("s", [])
    assert out.model == "opus" and b.calls == 0


def test_api_exception_falls_through():
    a = Fake("opus", RuntimeError("connection reset by peer"))
    b = Fake("kimi", "ok")
    out = chain(a, b).complete("s", [])
    assert out.model == "kimi" and not out.error


def test_rate_limit_falls_through_and_is_labelled():
    a = Fake("opus", RuntimeError("Error code: 429 - rate_limit_error"))
    b = Fake("kimi", "ok")
    c = chain(a, b)
    out = c.complete("s", [])
    assert out.model == "kimi"
    assert any("rate limit" in f for f in c.fallbacks)


def test_overloaded_falls_through():
    a = Fake("opus", RuntimeError("Error code: 529 - overloaded_error"))
    out = chain(a, Fake("kimi", "ok")).complete("s", [])
    assert out.model == "kimi"


def test_malformed_output_falls_through():
    """A reply that never becomes a valid program is a failure to answer."""
    a, b = Fake("opus", "malformed"), Fake("kimi", "ok")
    c = chain(a, b)
    out = c.complete("s", [])
    assert out.model == "kimi"
    assert any("malformed output" in f for f in c.fallbacks)


def test_timeout_falls_through():
    a = Fake("opus", TimeoutError("provider exceeded 70s"))
    c = chain(a, Fake("kimi", "ok"))
    out = c.complete("s", [])
    assert out.model == "kimi"
    assert any("timeout" in f for f in c.fallbacks)


def test_second_failure_reaches_the_third():
    a = Fake("opus", RuntimeError("429 rate limit"))
    b = Fake("kimi", "malformed")
    d = Fake("gptoss", "ok")
    out = chain(a, b, d).complete("s", [])
    assert out.model == "gptoss"


def test_every_provider_failing_returns_an_error_not_an_exception():
    c = chain(Fake("a", RuntimeError("boom")), Fake("b", RuntimeError("boom")))
    out = c.complete("s", [])
    assert out.error and len(c.fallbacks) == 2


def test_successful_completion_carries_the_fallback_trail():
    a = Fake("opus", RuntimeError("429 rate limit"))
    out = chain(a, Fake("kimi", "ok")).complete("s", [])
    assert out.fallbacks and "opus" in out.fallbacks[0]


def test_fallback_trail_resets_between_calls():
    a, b = Fake("opus", RuntimeError("429")), Fake("kimi", "ok")
    c = chain(a, b)
    c.complete("s", []); first = len(c.fallbacks)
    c.complete("s", [])
    assert len(c.fallbacks) == first


# --- malformed output gets a regeneration cycle before the chain moves on -----

class Flaky:
    """Malformed for the first n attempts, then valid — like a model that needs
    the typed repair message before it complies."""

    def __init__(self, name, bad_attempts):
        self.spec = spec(name)
        self.bad = bad_attempts
        self.attempts = 0

    def complete(self, system, messages, *, repairs=3):
        # mirrors Provider.complete: retry the same model with a repair message
        for attempt in range(repairs + 1):
            self.attempts += 1
            if self.attempts > self.bad:
                return Completion("plan", "print(1)", "raw", "test", self.spec.model, 0.1)
        return Completion("", "", "still bad", "test", self.spec.model, 0.1,
                          error="reply is not JSON")


def test_model_that_complies_after_repair_is_not_abandoned():
    """Three bad attempts then a good one must still be served by the first model."""
    a, b = Flaky("opus", bad_attempts=3), Fake("kimi", "ok")
    out = chain(a, b).complete("s", [])
    assert out.model == "opus" and a.attempts == 4 and b.calls == 0


def test_chain_moves_on_only_after_the_repair_budget_is_spent():
    a, b = Flaky("opus", bad_attempts=99), Fake("kimi", "ok")
    out = chain(a, b).complete("s", [])
    assert out.model == "kimi"
    assert a.attempts == 4, "the first model should get 1 try + 3 repairs"


def test_a_rate_limit_upstream_does_not_deny_the_next_model_its_repairs():
    """The reason one provider failed says nothing about the next one."""
    a = Fake("opus", RuntimeError("429 rate_limit_error"))
    b = Flaky("kimi", bad_attempts=3)
    out = chain(a, b).complete("s", [])
    assert out.model == "kimi" and b.attempts == 4


def test_total_failure_reports_every_model_it_tried():
    """When nothing answers, the stream must say what was tried and why."""
    a = Fake("opus", RuntimeError("429 rate_limit_error"))
    b = Fake("kimi", RuntimeError("connection reset"))
    d = Fake("gptoss", "malformed")
    c = chain(a, b, d)
    out = c.complete("s", [])
    assert out.error
    assert "every provider failed" in out.error
    assert "opus" in out.error and "kimi" in out.error and "gptoss" in out.error
    assert "rate limit" in out.error and "malformed output" in out.error
    assert len(out.fallbacks) == 3
