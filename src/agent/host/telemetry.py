"""OpenTelemetry emission, GenAI semantic conventions.

One decision cycle is one trace, rooted at an `invoke_agent` span:

    invoke_agent  alpaca_options_agent
      chat                      the model call that produced {thought, code}
      execute_tool  run_program the program, carrying the chat's tool call id
        execute_tool market.spot        capability calls the program made
        execute_tool options.enumerate
        ...
      chat                      the next round, if one was needed

This shape is the honest one for a code agent: the model makes one decision per
round and the program it wrote makes many tool calls. Only the program-run span
carries an id the model emitted, so only that span can be joined back to model
reasoning; the capability spans beneath it are recorded without it, which is what
actually happened.

Content is emitted only when OTEL_CAPTURE_CONTENT is true.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager, nullcontext

_ENABLED = False
_tracer = None
_provider = None

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "alpaca_options_agent")


def capture_content() -> bool:
    return os.environ.get("OTEL_CAPTURE_CONTENT", "").lower() in ("1", "true", "yes")


def setup(service_name: str | None = None, host: str | None = None) -> bool:
    """Idempotent. Returns whether emission is active."""
    global _ENABLED, _tracer, _provider
    if _ENABLED:
        return True
    host = host or os.environ.get("COLLECTOR_HOST")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or (
        f"http://{host}:4317" if host else None)
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _provider = TracerProvider(resource=Resource.create(
            {"service.name": service_name or SERVICE_NAME}))
        _provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=True)))
        trace.set_tracer_provider(_provider)
        _tracer = trace.get_tracer("alpaca-options-agent")
        _ENABLED = True
        return True
    except Exception as exc:                       # telemetry must never break trading
        print(f"[otel] disabled: {type(exc).__name__}: {exc}")
        return False


def enabled() -> bool:
    return _ENABLED


def shutdown() -> None:
    if _provider is not None:
        try:
            _provider.force_flush()
            _provider.shutdown()
        except Exception:
            pass


def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def _set(span, key: str, value) -> None:
    if value is not None:
        span.set_attribute(key, value)


# ---------------------------------------------------------------- span helpers

@contextmanager
def invoke_agent(name: str, *, trigger: str = "", cycle_id: str = ""):
    if not _ENABLED:
        yield None
        return
    with _tracer.start_as_current_span(f"invoke_agent {name}") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", name)
        _set(span, "alpaca.trigger", trigger)
        _set(span, "alpaca.cycle_id", cycle_id)
        yield span


@contextmanager
def chat(model: str, provider: str, *, round_no: int = 1):
    if not _ENABLED:
        yield None
        return
    with _tracer.start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", provider)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("alpaca.round", round_no)
        yield span


def finish_chat(span, *, model: str, input_tokens: int, output_tokens: int,
                reasoning_tokens: int | None, input_messages: list | None,
                output_messages: list | None) -> None:
    if span is None:
        return
    _set(span, "gen_ai.response.model", model)
    _set(span, "gen_ai.usage.input_tokens", input_tokens)
    _set(span, "gen_ai.usage.output_tokens", output_tokens)
    if reasoning_tokens:
        span.set_attribute("gen_ai.usage.reasoning.output_tokens", reasoning_tokens)
    if capture_content():
        if input_messages is not None:
            span.set_attribute("gen_ai.input.messages", json.dumps(input_messages))
        if output_messages is not None:
            span.set_attribute("gen_ai.output.messages", json.dumps(output_messages))


@contextmanager
def execute_tool(tool_name: str, call_id: str, *, arguments=None):
    if not _ENABLED:
        yield None
        return
    with _tracer.start_as_current_span(f"execute_tool {tool_name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", tool_name)
        span.set_attribute("gen_ai.tool.call.id", call_id)
        if capture_content() and arguments is not None:
            span.set_attribute("gen_ai.tool.call.arguments",
                               json.dumps(arguments, default=str)[:8000])
        yield span


def record_error(span, exc: BaseException) -> None:
    if span is None:
        return
    from opentelemetry.trace import Status, StatusCode
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))


# ---------------------------------------------------------------- message shapes

def user_message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "content": text}]}


def assistant_program_message(thought: str, code: str, call_id: str,
                              reasoning: str | None = None) -> dict:
    """The model's decision: public text plus one tool call that runs the program.

    Provider reasoning is normalised into a `reasoning` part and never merged into
    the public text.
    """
    parts: list[dict] = []
    if reasoning:
        parts.append({"type": "reasoning", "content": reasoning})
    if thought:
        parts.append({"type": "text", "content": thought})
    parts.append({"type": "tool_call", "id": call_id, "name": "run_program",
                  "arguments": {"code": code}})
    return {"role": "assistant", "parts": parts}


def tool_response_message(call_id: str, response) -> dict:
    return {"role": "assistant",
            "parts": [{"type": "tool_call_response", "id": call_id,
                       "response": response}]}
