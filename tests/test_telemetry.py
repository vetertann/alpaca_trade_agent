import json
import pytest
from agent.host import telemetry


def test_disabled_helpers_are_noops(monkeypatch):
    """Telemetry must never break trading, so every helper no-ops when off."""
    monkeypatch.setattr(telemetry, "_ENABLED", False)
    with telemetry.invoke_agent("a") as s:
        assert s is None
    with telemetry.chat("m", "p") as s:
        assert s is None
    with telemetry.execute_tool("t", "call_1") as s:
        assert s is None
    telemetry.finish_chat(None, model="m", input_tokens=1, output_tokens=1,
                          reasoning_tokens=0, input_messages=[], output_messages=[])
    telemetry.record_error(None, ValueError("x"))


def test_setup_returns_false_without_an_endpoint(monkeypatch):
    monkeypatch.setattr(telemetry, "_ENABLED", False)
    monkeypatch.delenv("COLLECTOR_HOST", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.setup() is False


def test_call_ids_are_unique():
    ids = {telemetry.new_call_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("call_") for i in ids)


def test_reasoning_is_a_separate_part_not_merged_into_text():
    m = telemetry.assistant_program_message("public plan", "print(1)", "call_1",
                                            reasoning="private chain")
    kinds = [p["type"] for p in m["parts"]]
    assert kinds == ["reasoning", "text", "tool_call"]
    text = next(p for p in m["parts"] if p["type"] == "text")
    assert "private chain" not in text["content"]


def test_program_message_carries_the_call_id():
    m = telemetry.assistant_program_message("t", "code", "call_abc")
    tc = next(p for p in m["parts"] if p["type"] == "tool_call")
    assert tc["id"] == "call_abc" and tc["name"] == "run_program"
    assert tc["arguments"]["code"] == "code"


def test_message_without_reasoning_omits_the_part():
    m = telemetry.assistant_program_message("t", "c", "call_1")
    assert [p["type"] for p in m["parts"]] == ["text", "tool_call"]


def test_tool_response_matches_the_call_id():
    r = telemetry.tool_response_message("call_1", {"ok": True})
    part = r["parts"][0]
    assert part["type"] == "tool_call_response" and part["id"] == "call_1"


def test_capture_content_defaults_off(monkeypatch):
    monkeypatch.delenv("OTEL_CAPTURE_CONTENT", raising=False)
    assert telemetry.capture_content() is False
    monkeypatch.setenv("OTEL_CAPTURE_CONTENT", "true")
    assert telemetry.capture_content() is True


def test_messages_serialize_to_json():
    m = telemetry.assistant_program_message("t", "print('x')", "call_1", "r")
    assert json.loads(json.dumps([m]))[0]["role"] == "assistant"
