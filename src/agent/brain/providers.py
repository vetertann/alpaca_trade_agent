"""Provider shim.

Two adapter families: OpenAI-compatible (Nebius, OpenAI, Featherless) and
Anthropic via its official SDK. `prompt_json` is the default tool mode -- every
reply is a single {thought, code} object -- which is what makes four providers one
integration rather than four.
"""
from __future__ import annotations

import ast
import json
import re
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from agent.brain.models import ModelSpec
from agent.config import get_key


@dataclass
class Completion:
    thought: str
    code: str
    raw: str
    provider: str
    model: str
    latency_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning: str = ""
    reasoning_tokens: int = 0
    attempts: int = 1
    fallbacks: list = field(default_factory=list)
    error: str | None = None


REPAIRS = 3          # one attempt plus three typed repairs


class ContractError(ValueError):
    """The reply was not a usable {thought, code} object."""


# Conditions where retrying the same provider is pointless: the next model in the
# chain is a better use of the cycle budget than a second attempt at a rate limit.
TERMINAL_MARKERS = ("rate limit", "rate_limit", "429", "quota", "insufficient_quota",
                    "overloaded", "capacity", "503", "529", "billing",
                    "authentication", "invalid_api_key", "permission")


def is_terminal(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in TERMINAL_MARKERS)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_contract(text: str) -> tuple[str, str]:
    if not text or not text.strip():
        raise ContractError("empty reply -- the budget was consumed before any content")
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as first:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            if start >= 0:
                raise ContractError(
                    "the reply was cut off before the JSON object closed -- the token "
                    "budget ran out. Write a shorter program.") from None
            raise ContractError(f"reply is not JSON: {cleaned[:120]!r}") from None
        try:
            obj = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            raise ContractError(
                f"the JSON object is malformed ({first.msg} at char {first.pos}). "
                "A likely cause is an unescaped newline or quote inside `code`; "
                "JSON-escape the source, or write a shorter program.") from None
    if not isinstance(obj, dict):
        raise ContractError("reply is not a JSON object")
    missing = {"thought", "code"} - set(obj)
    if missing:
        raise ContractError(f"missing key(s): {sorted(missing)}; got {sorted(obj)}")
    if not isinstance(obj["code"], str):
        raise ContractError("`code` must be a string of Python source")
    code = obj["code"]
    if not code.strip():
        raise ContractError("`code` is empty -- a cycle needs a program to run")
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise ContractError(
            f"`code` is not valid Python: {exc.msg} at line {exc.lineno}. "
            "Return runnable source, not a description of it.") from None
    return str(obj["thought"]), code


REPAIR = ("Your last reply did not satisfy the output contract ({error}).\n\n"
          "Reply with a single JSON object with exactly two keys:\n"
          '  "thought" — one or two sentences of plan\n'
          '  "code"    — executable Python source only\n\n'
          "No markdown fences, no text before or after the object. `code` must parse "
          "as Python; return the program itself, not a description of it.")


class Provider:
    def __init__(self, spec: ModelSpec, max_tokens: int = 8000,
                 request_timeout_s: float = 70.0):
        self.spec = spec
        self.max_tokens = max_tokens
        self.request_timeout_s = request_timeout_s
        self._client = None
        self._key: str | None = None

    @property
    def key(self) -> str:
        """Resolved on first use, so building a chain never fails on a later
        fallback's missing key."""
        if self._key is None:
            self._key = get_key(self.spec.key_name)
        return self._key

    # ---- adapters ----------------------------------------------------------
    def _anthropic(self, system, messages: list[dict]) -> tuple[str, dict]:
        import anthropic
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self.key, timeout=self.request_timeout_s, max_retries=0)
        # Non-streaming makes the SDK read timeout a real whole-response bound.
        # The old signal deadline is unavailable in the worker thread where live
        # cycles run, while a stream can keep a per-read timer alive indefinitely.
        msg = self._client.messages.create(
            model=self.spec.model, max_tokens=self.max_tokens, system=system,
            messages=messages, timeout=self.request_timeout_s, **self.spec.params)
        text = "".join(b.text for b in msg.content if b.type == "text")
        # thinking blocks are the provider's reasoning; keep them separate from text
        reasoning = "".join(getattr(b, "thinking", "") or ""
                            for b in msg.content if b.type == "thinking")
        # Anthropic reports these separately: input_tokens excludes both cache figures.
        return text, {"input_tokens": msg.usage.input_tokens,
                      "output_tokens": msg.usage.output_tokens,
                      "cached_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
                      "cache_write_tokens": getattr(
                          msg.usage, "cache_creation_input_tokens", 0) or 0,
                      "reasoning": reasoning,
                      "reasoning_tokens": len(reasoning) // 4 if reasoning else 0}

    def _openai_compatible(self, system, messages: list[dict]) -> tuple[str, dict]:
        if isinstance(system, list):        # flatten blocks for non-Anthropic
            system = "\n\n".join(b["text"] for b in system)
        from openai import OpenAI
        if self._client is None:
            self._client = OpenAI(
                api_key=self.key, base_url=self.spec.base_url,
                timeout=self.request_timeout_s, max_retries=0)
        kw = {"model": self.spec.model,
              "messages": [{"role": "system", "content": system}, *messages]}
        # OpenAI's newer models take max_completion_tokens; others take max_tokens.
        kw["max_completion_tokens" if self.spec.provider == "openai" else "max_tokens"] = \
            self.max_tokens
        kw |= self.spec.params
        r = self._client.chat.completions.create(
            **kw, timeout=self.request_timeout_s)
        u = r.usage
        cached = 0
        if u and getattr(u, "prompt_tokens_details", None):
            cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0
        msg = r.choices[0].message
        # OpenAI-compatible providers such as Kimi expose reasoning_content;
        # normalise it into the standard reasoning part, never into public text.
        reasoning = getattr(msg, "reasoning_content", None) or ""
        rtok = 0
        if u and getattr(u, "completion_tokens_details", None):
            rtok = getattr(u.completion_tokens_details, "reasoning_tokens", 0) or 0
        return (msg.content or ""), {
            "input_tokens": u.prompt_tokens if u else 0,
            "output_tokens": u.completion_tokens if u else 0,
            "cached_tokens": cached, "reasoning": reasoning,
            "reasoning_tokens": rtok or (len(reasoning) // 4 if reasoning else 0)}

    # ---- public ------------------------------------------------------------
    def complete(self, system, messages: list[dict], *, repairs: int = REPAIRS) -> Completion:
        """`system` may be a string or a list of cacheable blocks.

        A malformed reply is regenerated on the same model with a typed repair
        message before the chain gives up on it: one attempt plus `repairs` more.
        """
        convo = list(messages)
        last_error = None
        totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                  "cache_write_tokens": 0, "reasoning_tokens": 0}
        total_latency = 0.0
        for attempt in range(repairs + 1):
            t0 = time.monotonic()
            call = (self._anthropic if self.spec.provider == "anthropic"
                    else self._openai_compatible)
            text, usage = call(system, convo)
            dt_s = round(time.monotonic() - t0, 2)
            total_latency += dt_s
            for key in totals:
                totals[key] += int(usage.get(key) or 0)
            aggregate = {**usage, **totals}
            try:
                thought, code = parse_contract(text)
                return Completion(thought, code, text, self.spec.provider,
                                  self.spec.model, round(total_latency, 2),
                                  **aggregate, attempts=attempt + 1)
            except ContractError as exc:
                last_error = str(exc)
                if attempt == repairs:
                    return Completion("", "", text, self.spec.provider, self.spec.model,
                                      round(total_latency, 2), **aggregate,
                                      attempts=attempt + 1, error=last_error)
                convo = convo + [{"role": "assistant", "content": text or "(empty)"},
                                 {"role": "user", "content": REPAIR.format(error=last_error)}]
        raise AssertionError("unreachable")


class ChainProvider:
    """Tries each model in a role's chain until one answers.

    A missing key is not the only way a provider stops answering: it can time out,
    return an API error, or exhaust the repair budget without ever producing a valid
    {thought, code} object. All three fall through to the next model, and the
    substitution is visible in the returned Completion.
    """

    def __init__(self, specs: list[ModelSpec], max_tokens: int = 8000,
                 timeout_s: float = 70.0):
        if not specs:
            raise RuntimeError("empty provider chain")
        self.providers = [Provider(
            s, max_tokens=max_tokens,
            request_timeout_s=(s.request_timeout_s
                               if s.request_timeout_s is not None else timeout_s))
            for s in specs]
        self.timeout_s = timeout_s
        self.fallbacks: list[str] = []

    @property
    def spec(self) -> ModelSpec:
        return self.providers[0].spec

    def complete(self, system, messages: list[dict], *, repairs: int = REPAIRS) -> Completion:
        last: Completion | None = None
        self.fallbacks = []
        for i, prov in enumerate(self.providers):
            label = f"{prov.spec.provider}/{prov.spec.model}"
            try:
                with _deadline(getattr(prov, "request_timeout_s", self.timeout_s)):
                    # Every provider gets its own full repair budget. Why the
                    # previous one failed says nothing about whether this one can
                    # be talked into a valid program.
                    c = prov.complete(system, messages, repairs=repairs)
            except Exception as exc:                       # noqa: BLE001
                why = ("rate limit or quota" if is_terminal(exc)
                       else "timeout" if isinstance(exc, TimeoutError)
                       else type(exc).__name__)
                self.fallbacks.append(f"{label}: {why}: {str(exc)[:120]}")
                print(f"[providers] {label} did not answer ({why}); falling through")
                continue
            if c.error:
                self.fallbacks.append(f"{label}: malformed output: {c.error[:120]}")
                print(f"[providers] {label} never produced a valid program "
                      f"({c.error[:70]}); falling through")
                last = c
                continue
            if i:
                print(f"[providers] answered by {label} after {i} fallback(s)")
            c.fallbacks = list(self.fallbacks)
            return c
        trail = "; ".join(self.fallbacks) or "no providers configured"
        summary = (f"every provider failed after {repairs} repairs each — {trail}")
        if last is not None:
            last.error = summary
            last.fallbacks = list(self.fallbacks)
            return last
        return Completion("", "", "", self.spec.provider, self.spec.model, 0.0,
                          fallbacks=list(self.fallbacks), error=summary)


@contextmanager
def _deadline(seconds: float):
    """Wall-clock ceiling on one model call, so a hung provider cannot eat the cycle."""
    if not seconds or threading.current_thread() is not threading.main_thread():
        yield                       # signal-based alarms only work on the main thread
        return
    def _fire(signum, frame):
        raise TimeoutError(f"provider exceeded {seconds:.0f}s")
    old = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def for_role(role: str, *, dev: bool = False, max_tokens: int = 8000,
             timeout_s: float = 70.0) -> ChainProvider:
    from agent.brain.models import chain
    return ChainProvider(chain(role, dev=dev), max_tokens=max_tokens,
                         timeout_s=timeout_s)
