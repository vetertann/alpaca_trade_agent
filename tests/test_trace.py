from agent.host.trace import Trace


def test_cycle_records_in_order(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    cid = t.start_cycle({"name": "anchor"}, "hash1")
    t.preflight({"universe": {}})
    t.program(1, "plan", "print(1)", "nebius", "m", "v1", {"in": 1}, 1.0)
    t.evidence("out", [{"ns": "market", "fn": "spot"}], True, 0.5)
    t.outcome("NO_TRADE", "session closed")
    kinds = [r["kind"] for r in t.records()]
    assert kinds == ["TRIGGER", "PREFLIGHT", "PROGRAM", "EVIDENCE", "OUTCOME"]
    assert all(r["cycle"] == cid for r in t.records())


def test_program_is_stored_verbatim_and_hashed(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    t.start_cycle({}, "h")
    code = "x = 1\nprint(x)\n"
    t.program(1, "th", code, "p", "m", "v", {}, 0.1)
    rec = [r for r in t.records() if r["kind"] == "PROGRAM"][0]
    assert rec["code"] == code and len(rec["code_sha"]) == 16


def test_evidence_keeps_the_latest_sixteen_thousand_characters(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    t.start_cycle({}, "h")
    output = "a" * 1000 + "b" * 16000
    t.evidence(output, [], True, 0.1)
    rec = [r for r in t.records() if r["kind"] == "EVIDENCE"][0]
    assert rec["stdout"] == "b" * 16000


def test_evidence_records_explicit_timeout_state(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    t.start_cycle({}, "h")
    t.evidence("", [], False, 90.1, "SandboxTimeout", timed_out=True)
    rec = [r for r in t.records() if r["kind"] == "EVIDENCE"][0]
    assert rec["timed_out"] is True


def test_cycles_group(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    a = t.start_cycle({}, "h"); t.outcome("NO_TRADE")
    b = t.start_cycle({}, "h"); t.outcome("EXECUTED")
    assert set(t.cycles()) == {a, b}


def test_fill_strips_nested_jsonl_envelope_fields(tmp_path):
    t = Trace(tmp_path / "trace.jsonl")
    t.start_cycle({}, "h")

    t.fill({"kind": "STATE", "ts": "broker-time", "cycle": "ledger-cycle",
            "seq": 99, "order_id": "order-1", "filled_qty": 1})

    record = t.records()[-1]
    assert record["kind"] == "FILL"
    assert record["cycle"].startswith("cy_")
    assert record["order_id"] == "order-1"


def test_survives_reload(tmp_path):
    p = tmp_path / "trace.jsonl"
    t = Trace(p); t.start_cycle({}, "h"); t.note("hello", extra=1)
    assert len(Trace(p).records()) == 2
