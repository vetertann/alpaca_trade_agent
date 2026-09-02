from types import SimpleNamespace
import asyncio
import contextlib
import datetime as dt
import threading

from agent.brain import preflight
from agent.brain.loop import Trigger, TriggerState
from agent.brain.providers import Completion
from agent.sandbox.runner import RunResult
from agent.run import Agent
from agent.config import ET
from agent.host.risk_params import DEFAULT as RISK
from agent.host.thesis_store import ThesisStore


class Rest:
    def account(self):
        return {"equity": "100000", "cash": "100000",
                "options_buying_power": "100000", "options_trading_level": 3}

    def positions(self):
        return []


class Ledger:
    def risk_snapshot(self, positions):
        return {"structures": [], "realised_loss": 0.0, "premium_at_risk": 0.0}


class Executor:
    def __init__(self):
        self._staged = None
        self._consumed = set()

    @property
    def latest_staged(self):
        return self._staged

    def begin_cycle(self, cycle_id):
        pass

    def begin_program(self, program_id):
        pass

    def discard_staged(self):
        self._staged = None


class Theses:
    def list(self, status="open"):
        return []

    def outcomes(self, limit=20):
        return []


class Trace:
    cycle_id = None

    def __init__(self):
        self.final = None

    def start_cycle(self, trigger, bundle_hash):
        self.cycle_id = "cycle-test"

    def outcome(self, outcome, reason):
        self.final = (outcome, reason)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Provider:
    spec = SimpleNamespace(model="test-model", provider="test-provider")

    def __init__(self):
        self.messages = []
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        self.messages.append(messages)
        code = f"program_{self.calls} = True"
        raw = '{"thought":"test","code":"' + code + '"}'
        return Completion("test", code, raw, "test-provider", "test-model", 0.0)


class Sandbox:
    def __init__(self, agent):
        self.agent = agent
        self.calls = 0
        self.state_manifest = {"persisted": [], "dropped": [], "total_bytes": 0}

    def reset(self):
        self.state_manifest = {"persisted": [], "dropped": [], "total_bytes": 0}

    def run(self, code, bundle):
        self.calls += 1
        if self.calls == 1:
            self.state_manifest = {
                "persisted": [{"name": "simulation", "type": "dict", "bytes": 32}],
                "dropped": [], "total_bytes": 32}
            return RunResult(True, '{"tail_loss": 0.27}', "",
                             state_manifest=self.state_manifest)
        self.agent.caps.dispatch(
            "decision", "no_trade", ["tail loss invalidates the edge"], {})
        return RunResult(True, "declined", "", state_manifest=self.state_manifest)


class EndlessContinuationSandbox(Sandbox):
    def run(self, code, bundle):
        self.calls += 1
        self.state_manifest = {
            "persisted": [{"name": f"simulation_{self.calls}", "type": "dict",
                           "bytes": 32}],
            "dropped": [], "total_bytes": 32}
        return RunResult(True, f'{{"simulation": {self.calls}}}', "",
                         state_manifest=self.state_manifest)


class PriceConditionMissSandbox(Sandbox):
    def run(self, code, bundle):
        self.calls += 1
        self.agent.caps._trading_result = {
            "status": "condition_not_met",
            "reason": "fresh executable credit below authorization floor",
        }
        return RunResult(True, "price boundary missed", "",
                         state_manifest=self.state_manifest)


class IncompletePriceProtocolSandbox(Sandbox):
    def run(self, code, bundle):
        self.calls += 1
        self.agent.caps._trading_result = {
            "status": "needs_price_authorization",
            "next": "use execute_if",
        }
        return RunResult(True, "conditional confirmation still required", "",
                         state_manifest=self.state_manifest)


class OrphanThesisSandbox(EndlessContinuationSandbox):
    def run(self, code, bundle):
        if self.calls == 0:
            self.agent.theses.open(
                "unsubmitted draft", "SPY", exit_profit="50%",
                exit_invalidation="no edge", exit_time="2026-09-03 15:45 ET")
        return super().run(code, bundle)


class FailedStage:
    passed = False
    verified = SimpleNamespace(
        nonce="blocked", intent=SimpleNamespace(
            underlying="SPY", family="vertical_call", legs=[]))
    results = [SimpleNamespace(name="spread", passed=False)]

    def checklist(self):
        return "FAIL  spread: too wide"


class BlockedStageSandbox(Sandbox):
    def run(self, code, bundle):
        self.calls += 1
        if self.calls == 1:
            self.agent.executor._staged = FailedStage()
            self.agent.caps._trading_result = {"status": "staged", "passed": False}
            self.state_manifest = {
                "persisted": [{"name": "intent", "type": "dict", "bytes": 120}],
                "dropped": [], "total_bytes": 120}
            return RunResult(True, "candidate priced", "",
                             state_manifest=self.state_manifest)
        self.agent.caps.dispatch(
            "decision", "no_trade", ["spread gate rejected the candidate"], {})
        return RunResult(True, "declined", "", state_manifest=self.state_manifest)


def make_agent(monkeypatch, sandbox_type=Sandbox):
    bundle = {"bundle_hash": "bundle", "universe": {}, "clock": {"now_et": "now"}}
    monkeypatch.setattr(preflight, "build", lambda *args, **kwargs: bundle)

    agent = Agent.__new__(Agent)
    agent.rest = Rest()
    agent.series = object()
    agent.theses = Theses()
    agent.executor = Executor()
    agent.ledger = Ledger()
    agent.params = object()
    agent.trace = Trace()
    agent.provider = Provider()
    agent.sandbox = sandbox_type(agent)
    agent.triggers = TriggerState()
    agent.expiries = ["2026-09-03"]
    agent.previous_bundle = None
    agent.history = []
    agent.blocked = []
    agent.caps = None
    agent._reconcile_execution = lambda: None
    agent._trading_day = lambda: True
    return agent


def test_successful_simulation_can_request_a_fresh_model_decision(monkeypatch):
    agent = make_agent(monkeypatch)

    outcome = agent._cycle_inner(Trigger("simulation", "test continuation"))

    assert outcome == "NO_TRADE"
    assert agent.provider.calls == agent.sandbox.calls == 2
    second_request = agent.provider.messages[1]
    assert len(second_request) == 1 and second_request[0]["role"] == "user"
    assert '"tail_loss": 0.27' in second_request[-1]["content"]
    assert "without a terminal submission" in second_request[-1]["content"]
    assert "`simulation`: dict" in second_request[-1]["content"]
    assert sum("# Current persisted program state" in message["content"]
               for message in second_request) == 1
    assert agent.trace.final == ("NO_TRADE", "tail loss invalidates the edge")


def test_continuation_cannot_escape_the_three_program_budget(monkeypatch):
    agent = make_agent(monkeypatch, EndlessContinuationSandbox)

    outcome = agent._cycle_inner(Trigger("simulation", "bounded continuation"))

    assert outcome == "ERROR"
    assert agent.provider.calls == agent.sandbox.calls == 3
    third_request = agent.provider.messages[2]
    state_turns = [message["content"] for message in third_request
                   if "# Current persisted program state" in message["content"]]
    assert len(state_turns) == 1
    assert "`simulation_2`: dict" in state_turns[0]
    assert "simulation_1" not in state_turns[0]
    assert agent.trace.final == (
        "ERROR", "round budget exhausted without a terminal decision")


def test_final_price_condition_miss_is_a_clean_no_trade(monkeypatch):
    agent = make_agent(monkeypatch, PriceConditionMissSandbox)

    outcome = agent._cycle_inner(Trigger("simulation", "bounded price review"))

    assert outcome == "NO_TRADE"
    assert agent.provider.calls == agent.sandbox.calls == 3
    assert agent.trace.final == (
        "NO_TRADE",
        "fresh-price authorization ended without qualifying; no order submitted")


def test_safe_presubmit_protocol_exhaustion_is_incomplete_not_runtime_error(monkeypatch):
    agent = make_agent(monkeypatch, IncompletePriceProtocolSandbox)

    outcome = agent._cycle_inner(Trigger("session_anchor", "11:00 ET anchor"))

    assert outcome == "INCOMPLETE"
    assert agent.provider.calls == agent.sandbox.calls == 3
    assert agent.trace.final[0] == "INCOMPLETE"
    assert "safe pre-submit protocol state" in agent.trace.final[1]
    assert "no order submitted" in agent.trace.final[1]


def test_terminal_cycle_closes_new_thesis_without_an_order(monkeypatch, tmp_path):
    agent = make_agent(monkeypatch, OrphanThesisSandbox)
    agent.theses = ThesisStore(tmp_path / "theses.jsonl")

    outcome = agent._cycle_inner(Trigger("simulation", "orphan cleanup"))

    assert outcome == "ERROR"
    theses = agent.theses.list(status=None)
    assert len(theses) == 1
    assert theses[0].status == "closed"
    assert any("without a submitted entry" in note for note in theses[0].notes)


def test_failed_stage_is_observed_before_explicit_no_trade(monkeypatch):
    agent = make_agent(monkeypatch, BlockedStageSandbox)

    outcome = agent._cycle_inner(Trigger("simulation", "review blocked stage"))

    assert outcome == "NO_TRADE"
    assert agent.provider.calls == 2
    second_request = agent.provider.messages[1]
    assert "FAIL  spread: too wide" in second_request[-1]["content"]
    assert "not submitted" in second_request[-1]["content"]
    assert "`intent`: dict" in second_request[-1]["content"]
    assert agent.trace.final == (
        "NO_TRADE", "spread gate rejected the candidate")


def test_classifier_keeps_nonterminal_results_in_the_loop(monkeypatch):
    agent = make_agent(monkeypatch)
    assert agent._classify("simulation output", None) == (
        "CONTINUE", "program completed without a terminal decision")
    assert agent._classify(
        "Final program will decide between a candidate and NO_TRADE", None
    ) == ("CONTINUE", "program completed without a terminal decision")

    blocked = SimpleNamespace(
        passed=False, verified=SimpleNamespace(nonce="unsubmitted"))
    assert agent._classify("", blocked, trading_result={"status": "staged"})[0] == \
        "NEEDS_REVIEW"
    assert agent._classify("", None, trading_result={"status": "proposed"}) == (
        "PROPOSED", "propose mode completed; no broker order submitted")
    assert agent._classify("", None, trading_result={"status": "submitted_close"}) == (
        "SUBMITTED", "closing order submitted; position remains open until filled")


def test_legacy_scenario_close_is_recovered_reopened_and_retried(tmp_path):
    ledger = __import__("agent.host.ledger", fromlist=["ExecutionLedger"]).ExecutionLedger(
        tmp_path / "execution.jsonl")
    theses = ThesisStore(tmp_path / "theses.jsonl")
    thesis = theses.open("risk repair", "QQQ", exit_profit="50%",
                         exit_invalidation="scenario breach",
                         exit_time="2026-09-02 15:45 ET")
    theses.close(thesis.thesis_id, reason="premature model closure")
    structure = {"structure_id": "sid-qqq", "thesis_id": thesis.thesis_id,
                 "underlying": "QQQ", "family": "iron_condor", "qty": 4,
                 "legs": []}
    ledger.record_order(
        order_id="old-close", client_order_id="x-old", structure_id="sid-qqq",
        purpose="exit", thesis_id=thesis.thesis_id, underlying="QQQ",
        family="iron_condor", legs=[], qty=6, signed_limit_price=1.62,
        max_loss_per_unit=159.1, cycle_id="old-cycle",
        reason="Risk-reducing exit under breached correlated scenario limit",
        status="canceled")

    calls = []

    class RetryExecutor:
        def close_structure(self, got, **kwargs):
            calls.append((got, kwargs))
            return {"status": "submitted_close", "order_id": "new-close",
                    "qty": got["qty"], "limit_price": 1.70}

    agent = Agent.__new__(Agent)
    agent.ledger = ledger
    agent.theses = theses
    agent.executor = RetryExecutor()
    agent.trace = Trace()

    acted = agent._retry_mandatory_exits(
        [structure], dt.datetime(2026, 9, 1, 11, 45, tzinfo=ET))

    assert acted == ["sid-qqq: submitted_close"]
    assert calls[0][0]["qty"] == 4  # only the remaining broker quantity
    assert calls[0][1]["must_fill"] is True
    assert theses.get(thesis.thesis_id).status == "open"
    assert ledger.active_exit_intents()[0]["source"] == "legacy_scenario_repair"


def test_mandatory_exit_completes_only_when_structure_is_flat(tmp_path):
    from agent.host.ledger import ExecutionLedger

    ledger = ExecutionLedger(tmp_path / "execution.jsonl")
    theses = ThesisStore(tmp_path / "theses.jsonl")
    thesis = theses.open("repair", "QQQ", exit_profit="p",
                         exit_invalidation="i",
                         exit_time="2026-09-02 15:45 ET")
    ledger.arm_exit_intent(structure_id="sid", thesis_id=thesis.thesis_id,
                           reason="mandatory exit", source="test")
    agent = Agent.__new__(Agent)
    agent.ledger = ledger
    agent.theses = theses
    agent.executor = SimpleNamespace()
    agent.trace = Trace()

    acted = agent._retry_mandatory_exits(
        [], dt.datetime(2026, 9, 1, 11, 46, tzinfo=ET))

    assert acted == []
    assert ledger.active_exit_intents() == []
    assert theses.get(thesis.thesis_id).status == "closed"


def test_canceled_unfilled_entry_thesis_is_closed_during_reconciliation(tmp_path):
    agent = Agent.__new__(Agent)
    agent.theses = ThesisStore(tmp_path / "theses.jsonl")
    thesis = agent.theses.open("draft", "QQQ", exit_profit="p",
                               exit_invalidation="i",
                               exit_time="2026-09-03 15:45 ET")
    agent.theses.update(thesis.thesis_id, order_ids=["order-1"])

    class ReconcileExecutor:
        def reconcile_orders(self, cancel_after_s=60): return []
        def entry_blockers(self): return []

    class ReconcileLedger:
        def descriptors(self): return {}
        def states(self):
            return {"order-1": {"status": "canceled", "filled_qty": 0}}
        def structure_summaries(self): return {}

    agent.executor = ReconcileExecutor()
    agent.ledger = ReconcileLedger()
    agent.trace = Trace()

    agent._reconcile_execution()

    assert agent.theses.get(thesis.thesis_id).status == "closed"


def test_background_reconciliation_does_not_close_in_progress_draft(tmp_path):
    agent = Agent.__new__(Agent)
    agent.theses = ThesisStore(tmp_path / "theses.jsonl")
    thesis = agent.theses.open("program draft", "QQQ", exit_profit="p",
                               exit_invalidation="i",
                               exit_time="2026-09-03 15:45 ET")

    class ReconcileExecutor:
        def reconcile_orders(self, cancel_after_s=60): return []
        def entry_blockers(self): return []

    class ReconcileLedger:
        def descriptors(self): return {}
        def states(self): return {}
        def structure_summaries(self): return {}

    agent.executor = ReconcileExecutor()
    agent.ledger = ReconcileLedger()
    agent.trace = Trace()

    agent._reconcile_execution(reconcile_drafts=False)

    assert agent.theses.get(thesis.thesis_id).status == "open"


def test_exit_monitor_runs_while_decision_cycle_is_blocked(monkeypatch):
    monkeypatch.setattr("agent.run.session_state", lambda *_args: "ACTIVE")
    agent = Agent.__new__(Agent)
    agent._cycle_lock = asyncio.Lock()
    agent._current_trading_day = True
    agent.trace = Trace()
    release_model = threading.Event()
    exit_ran = threading.Event()
    sweep_calls = []

    def blocked_cycle(_trigger):
        release_model.wait(timeout=2)
        return "NO_TRADE"

    def sweep(now, **kwargs):
        sweep_calls.append((now, kwargs))
        exit_ran.set()
        return []

    agent._cycle_blocking = blocked_cycle
    agent.sweep_exits = sweep

    async def scenario():
        cycle_task = asyncio.create_task(
            agent.cycle(Trigger("test", "blocked provider")))
        monitor_task = asyncio.create_task(agent._exit_monitor(0.01))
        try:
            observed = await asyncio.wait_for(
                asyncio.to_thread(exit_ran.wait, 0.5), timeout=1)
            assert observed is True
            assert cycle_task.done() is False
            assert sweep_calls[0][1] == {
                "observe_adaptive": True,
                "reconcile_drafts": False,
                "force_snapshot": True,
            }
        finally:
            release_model.set()
            assert await asyncio.wait_for(cycle_task, timeout=1) == "NO_TRADE"
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

    asyncio.run(scenario())


def test_orderless_thesis_is_preserved_while_broker_exposure_remains(tmp_path):
    agent = Agent.__new__(Agent)
    agent.theses = ThesisStore(tmp_path / "theses.jsonl")
    thesis = agent.theses.open("live structure", "QQQ", exit_profit="p",
                               exit_invalidation="i",
                               exit_time="2026-09-03 15:45 ET")

    class ReconcileExecutor:
        def reconcile_orders(self, cancel_after_s=60): return []
        def entry_blockers(self): return []

    class ReconcileLedger:
        def descriptors(self): return {}
        def states(self): return {}
        def structure_summaries(self):
            return {"sid": {"structure_id": "sid", "thesis_id": thesis.thesis_id,
                            "ledger_open_qty": 2}}

    agent.executor = ReconcileExecutor()
    agent.ledger = ReconcileLedger()
    agent.trace = Trace()

    agent._reconcile_execution()

    assert agent.theses.get(thesis.thesis_id).status == "open"


def test_exit_sweep_uses_the_durable_thesis_deadline():
    structure = {
        "structure_id": "spread-1", "thesis_id": "th-1",
        "cost_basis": -200, "unrealized_pl": 0, "premium_at_risk": 300,
        "qty": 1, "legs": [
            {"side": "sell", "expiry": "2026-09-01"},
            {"side": "buy", "expiry": "2026-09-01"},
        ],
    }

    class ExitRest:
        def positions(self):
            return []

    class ExitLedger:
        def risk_snapshot(self, _positions):
            return {"structures": [structure]}

    class ExitTheses:
        def get(self, thesis_id):
            assert thesis_id == "th-1"
            return SimpleNamespace(exit_at="2026-09-01T15:30:00-04:00",
                                   exit_time="2026-09-01 15:30 ET")

    class ExitExecutor:
        def close_structure(self, got, reason, now):
            assert got is structure
            return {"status": "submitted", "reason": reason}

    agent = Agent.__new__(Agent)
    agent.rest = ExitRest()
    agent.ledger = ExitLedger()
    agent.theses = ExitTheses()
    agent.executor = ExitExecutor()
    agent.params = RISK
    agent.trace = Trace()
    agent._reconcile_execution = lambda: None
    agent._detect_assignment = lambda positions: None

    acted = agent.sweep_exits(dt.datetime(2026, 9, 1, 15, 30, tzinfo=ET))

    assert len(acted) == 1
    assert "thesis time stop" in acted[0]


def test_expiry_exit_ledger_reason_names_failed_settlement_safeguard():
    structure = {
        "structure_id": "spread-settle", "thesis_id": "",
        "cost_basis": 200, "unrealized_pl": 0, "premium_at_risk": 200,
        "qty": 1, "legs": [{"side": "buy", "expiry": "2026-09-01"}],
        "settlement_authorization": {
            "authorized": False,
            "reason": "nearest short strike is inside the required distance",
            "standing_rule": {"min_short_distance_points": 3},
        },
    }
    captured = []

    class Executor:
        def close_structure(self, got, **kwargs):
            captured.append(kwargs)
            return {"status": "submitted_close"}

    agent = Agent.__new__(Agent)
    agent.executor = Executor()
    agent.theses = SimpleNamespace(get=lambda _thesis_id: None)
    agent.params = RISK
    agent.trace = Trace()

    acted = agent._evaluate_snapshot_exits(
        {"structures": [structure]},
        dt.datetime(2026, 9, 1, 15, 20, tzinfo=ET), observe_adaptive=False)

    assert acted
    assert "expiry-day mandatory liquidation" in captured[0]["reason"]
    assert "nearest short strike" in captured[0]["reason"]
    assert captured[0]["mandatory_source"] == "expiry_day_liquidation"
