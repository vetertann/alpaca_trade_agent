"""Entry point. Tier 0 runs continuously; Tier 2 runs when a predicate fires."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import math
import os
import threading
import time
from collections import deque
from dataclasses import replace

from agent.brain import preflight, prompt, providers
from agent.brain.shadow import ShadowRunner
from agent.brain.loop import (ANCHORS, DEBOUNCE_SECONDS, MAX_CYCLES_PER_SESSION,
                              Trigger, TriggerState,
                              entries_allowed, position_exit_due, session_state)
from agent.config import ET, WINDOW_CLOSE, load_env, profile
from agent.host.capabilities import Capabilities
from agent.host.action_triggers import ActionTriggerStore, intent_from_dict
from agent.host.execution import Executor
from agent.host.exit_policy import ExitPolicyStore
from agent.host.ledger import ExecutionLedger, TERMINAL_STATUSES
from agent.host import portfolio, portfolio_risk as portfolio_stress, runtime_state
from agent.host.rest import Rest
from agent.host.risk_params import DEFAULT as RISK
from agent.host.series import RollingSeries
from agent.host import telemetry
from agent.host.streams import Handlers, StreamSet
from agent.host.thesis_store import ThesisStore
from agent.host.trace import Trace
from agent.sandbox.runner import Sandbox, hint_for

def _failing_line(code: str, stderr: str) -> str:
    """Pull the source line the traceback blamed, so the repair can name it."""
    import re
    match = re.search(r'File "<program>", line (\d+)', stderr or "")
    if not match:
        return ""
    lines = code.splitlines()
    index = int(match.group(1)) - 1
    return lines[index].strip() if 0 <= index < len(lines) else ""


MEGACAPS = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META"]
TRADED = ["SPY", "QQQ", "IWM"]
CYCLE_BUDGET_S = 90.0
MAX_ROUNDS = 3
CONFIRMATION_REFRESH_AFTER_S = 15.0
TRIGGER_IV_REFRESH_SECONDS = 60.0
TRIGGER_DATA_RETRY_SECONDS = 5.0
MAX_BLOCKED_TRIGGER_ESCALATIONS_PER_SESSION = 3

# Fire-time admission failures have different lifecycle semantics.  A stale quote
# or temporarily wide spread is not evidence that the authorization itself is
# unsafe, so the host leaves it armed and retries with backoff.  Only a genuine
# risk refusal earns the terminal blocked_risk state and an urgent model review.
TRANSIENT_ACTION_TRIGGER_GATES = frozenset({
    "quote_valid", "quote_age", "spread",
})
RISK_ACTION_TRIGGER_GATES = frozenset({
    "portfolio_scenario", "risk_budget", "buying_power", "concentration",
    "economics", "quantity_headroom",
})


def _eligible_expiries(rows: list[dict], today: dt.date) -> list[str]:
    """Every future active expiry returned by the broker.

    Expiry is not the economic horizon.  Later contracts are valued at the
    official equity mark, so a local calendar cutoff would discard legitimate
    delta/vega opportunities without reducing execution risk.
    """
    return sorted({str(c["expiration_date"]) for c in rows
                   if today <= dt.date.fromisoformat(str(c["expiration_date"]))})


class Agent:
    def __init__(self, profile_name: str, mode: str, dev_models: bool,
                 run_dir: str = ".run", robust_risk_pct: float = 0.04,
                 scenario_risk_pct: float = 0.04):
        self.run_dir = run_dir
        self.prof = profile(profile_name)
        self.profile_name = profile_name
        self.mode = mode
        if not 0 < robust_risk_pct <= 0.15:
            raise ValueError("robust_risk_pct must be in (0, 0.15]")
        if not 0 < scenario_risk_pct <= 0.25:
            raise ValueError("scenario_risk_pct must be in (0, 0.25]")
        self.robust_risk_pct = robust_risk_pct
        self.scenario_risk_pct = scenario_risk_pct
        # Execute-mode account and order lifecycle traffic uses Alpaca's official
        # CLI, satisfying the competition integration requirement. Market data and
        # contract metadata remain direct read-only API calls.
        execution_transport = "cli" if mode == "execute" else "rest"
        self.rest = Rest(self.prof, execution_transport=execution_transport)
        self.series = RollingSeries()
        self.theses = ThesisStore(f"{run_dir}/theses.jsonl")
        self.trace = Trace(f"{run_dir}/trace.jsonl")
        # One host-owned value feeds both the prompt description and the actual
        # executor ceiling; the two can no longer drift apart.
        self.params = replace(
            RISK, robust_evidence_risk_pct=robust_risk_pct * 100.0,
            max_correlated_scenario_loss_pct=scenario_risk_pct * 100.0)
        self.ledger = ExecutionLedger(f"{run_dir}/execution.jsonl")
        self.exit_policies = ExitPolicyStore(f"{run_dir}/exit_policies.jsonl")
        self.action_triggers = ActionTriggerStore(
            f"{run_dir}/action_triggers.jsonl")
        self.executor = Executor(self.rest, self.params, profile_name, mode=mode,
                                 ledger=self.ledger, enforce_entry_risk=True)
        self.triggers = TriggerState()
        self.sandbox = Sandbox(self._dispatch, workdir=run_dir, timeout_s=CYCLE_BUDGET_S)
        self.provider = providers.for_role("decision", dev=dev_models, max_tokens=8000)
        self.previous_bundle: dict | None = None
        self._portfolio_lock = threading.RLock()
        self.portfolio_history: deque[dict] = deque(maxlen=180)
        self._latest_portfolio_snapshot: dict | None = None
        self._trigger_universe_cache: dict = {}
        self._last_trigger_iv_refresh: dt.datetime | None = None
        # A compact record of what this agent already decided. Kept in memory rather
        # than re-read from the trace, so the bundle never depends on file parsing.
        self.history: list[dict] = []
        self.blocked: list[dict] = []
        # Fixed policies alongside the agent. They place no orders -- all
        # competition trading happens in the one account.
        self.shadow = ShadowRunner(path=f"{run_dir}/shadow.jsonl")
        self.caps: Capabilities | None = None
        self.streams: StreamSet | None = None
        self.expiries: list[str] = []
        self._cycle_lock = asyncio.Lock()
        self._exit_sweep_lock = threading.RLock()
        self.starting_equity: float | None = None
        self.starting_equity_captured_at: str | None = None
        self.restart_rebaseline_needed = False
        self.startup_analysis_needed = False
        self._pending_triggers: deque[tuple[str, Trigger]] = deque(maxlen=32)
        self._pending_trigger_keys: set[str] = set()
        self._last_position_quantities: dict[str, float] | None = None
        self.runtime_state_age_seconds: float | None = None
        try:
            self.series.restore(f"{run_dir}/series.json")
            self._restore_runtime_state()
        except Exception as exc:
            self.trace.error("runtime_state_restore", exc)

    def _restore_runtime_state(self, now: dt.datetime | None = None) -> bool:
        """Restore only session-valid scheduling state; drafts are never serialized."""
        raw = runtime_state.read(f"{self.run_dir}/runtime_state.json")
        if raw is None:
            return False
        now = (now or dt.datetime.now(ET)).astimezone(ET)
        self.starting_equity = (float(raw["starting_equity"])
                                if raw.get("starting_equity") is not None else None)
        self.starting_equity_captured_at = raw.get("starting_equity_captured_at")
        # Outcome summaries are useful only under the policy that produced them.
        # A horizon-policy deployment must not leave eight old single-expiry
        # decisions in the next prompt merely because the process restarted in the
        # same session.
        same_prompt_policy = (
            raw.get("prompt_policy_version") == self._prompt_policy_version())
        self.history = (list(raw.get("history") or [])[-8:]
                        if same_prompt_policy else [])
        self.blocked = list(raw.get("blocked") or [])[-8:]
        saved_at = dt.datetime.fromisoformat(str(raw["saved_at"])).astimezone(ET)
        same_session = raw.get("session_date") == now.date().isoformat()
        if not same_session:
            if hasattr(self, "series"):
                self.series = RollingSeries()  # intraday returns never cross overnight
            self.triggers = TriggerState()
            self.previous_bundle = None
            return True

        restored = TriggerState.from_json(dict(raw.get("triggers") or {}))
        age = max((now - saved_at).total_seconds(), 0.0)
        self.runtime_state_age_seconds = round(age, 1)
        if age <= DEBOUNCE_SECONDS:
            self.triggers = restored
            self.previous_bundle = raw.get("previous_bundle")
            self.portfolio_history = deque(
                list(raw.get("portfolio_history") or [])[-60:], maxlen=180)
            self._latest_portfolio_snapshot = (
                self.portfolio_history[-1] if self.portfolio_history else None)
        else:
            # Keep the session count, but do not call an outage-sized move a normal
            # incremental trigger.  Reconstruct anchors already passed so restart at
            # 11:05 cannot replay the 09:45 anchor.
            restored.last_cycle_at = None
            restored.baseline = {}
            restored.portfolio_baseline = {}
            restored.last_anchor_fired = None
            for anchor in ANCHORS:
                if now.time() >= anchor:
                    restored.last_anchor_fired = anchor
            self.triggers = restored
            self.previous_bundle = None
            self.portfolio_history = deque(maxlen=180)
            self.restart_rebaseline_needed = session_state(now) != "CLOSED"
        return True

    def _checkpoint_runtime_state(self, now: dt.datetime | None = None) -> None:
        if not getattr(self, "run_dir", None):
            return
        now = (now or dt.datetime.now(ET)).astimezone(ET)
        runtime_state.write(f"{self.run_dir}/runtime_state.json", {
            "saved_at": now.isoformat(), "session_date": now.date().isoformat(),
            "prompt_policy_version": self._prompt_policy_version(),
            "starting_equity": self.starting_equity,
            "starting_equity_captured_at": self.starting_equity_captured_at,
            "triggers": self.triggers.to_json(),
            "previous_bundle": self.previous_bundle,
            "portfolio_history": self._portfolio_history_rows()[-60:],
            "history": self.history[-8:], "blocked": self.blocked[-8:],
            # Deliberately absent: executor._staged, nonces and sandbox state.
        })
        self.runtime_state_age_seconds = 0.0

    def _prompt_policy_version(self) -> str:
        return prompt.prompt_version(
            robust_risk_pct=getattr(
                self, "robust_risk_pct", prompt.DEFAULT_ROBUST_RISK_PCT),
            scenario_risk_pct=getattr(
                self, "scenario_risk_pct", prompt.DEFAULT_SCENARIO_RISK_PCT))

    def _capture_starting_equity(self, account: dict | None = None) -> None:
        if self.starting_equity is not None:
            return
        account = account or self.rest.account()
        equity = float(account.get("equity") or 0)
        if (self.profile_name == "competition" and self.mode == "execute"
                and not self.ledger.descriptors() and not self.ledger.executions()
                and abs(equity - 100_000.0) > 0.01):
            raise RuntimeError(
                f"competition account must start at $100,000; broker reports ${equity:,.2f}")
        self.starting_equity = 100_000.0 if self.profile_name == "competition" else equity
        self.starting_equity_captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self._checkpoint_runtime_state()

    # ---- Tier 0 -----------------------------------------------------------
    def _on_equity(self, sym, bid, ask, when):
        if bid > 0 and ask > 0:
            self.series.observe(sym, (bid + ask) / 2, when)

    def _on_option(self, sym, bid, ask, when):
        if bid > 0 and ask > 0:
            self.series.observe(sym, (bid + ask) / 2, when)

    def _on_news(self, article):
        syms = set(article.get("symbols") or [])
        relevant = syms & set(TRADED)
        if relevant:
            headline = str(article.get("headline") or "market-moving headline")
            self.trace.note("news_position_keyed", headline=article.get("headline"),
                            symbols=sorted(syms))
            self._queue_trigger(
                Trigger("relevant_news",
                        f"news for {', '.join(sorted(relevant))}: {headline[:180]}"),
                key="news:" + hashlib.sha256(
                    (headline + "|" + ",".join(sorted(relevant))).encode()
                ).hexdigest()[:16])

    def _on_trade_update(self, data):
        order = data.get("order") or {}
        event = str(data.get("event") or "").lower()
        order_id = str(order.get("id") or "unknown")
        self.trace.note("trade_update", event=event,
                        order_id=order.get("id"), status=order.get("status"))
        if order.get("id") in self.ledger.descriptors():
            try:
                state = self.ledger.record_state(order)
                if state.get("delta_filled_qty"):
                    self.trace.fill(state)
            except Exception as exc:
                self.trace.error("trade_update_reconcile", exc)
        if event in ("fill", "partial_fill"):
            self._queue_trigger(
                Trigger("fill_update", f"{event} received for order {order_id}",
                        exempt_from_debounce=True),
                key=f"fill:{order_id}")
        elif event in ("assignment", "exercise"):
            self._queue_trigger(
                Trigger("assignment", f"{event} received for order {order_id}",
                        exempt_from_debounce=True),
                key=f"assignment:{order_id}")

    def _on_error(self, feed, exc):
        self.trace.error(f"stream:{feed}", exc)

    def _queue_trigger(self, trigger: Trigger, *, key: str) -> None:
        """Coalesce stream events until the ten-second dispatcher consumes them."""
        if not hasattr(self, "_pending_triggers"):
            self._pending_triggers = deque(maxlen=32)
            self._pending_trigger_keys = set()
        if key in self._pending_trigger_keys:
            return
        if len(self._pending_triggers) == self._pending_triggers.maxlen:
            old_key, _ = self._pending_triggers.popleft()
            self._pending_trigger_keys.discard(old_key)
        self._pending_triggers.append((key, trigger))
        self._pending_trigger_keys.add(key)

    def _pop_event_trigger(self, now: dt.datetime) -> Trigger | None:
        while self._pending_triggers:
            key, trigger = self._pending_triggers.popleft()
            self._pending_trigger_keys.discard(key)
            if self.triggers.cycles_this_session >= MAX_CYCLES_PER_SESSION:
                self.trace.note("trigger_suppressed", trigger=trigger.name,
                                reason="session cycle cap reached")
                continue
            if trigger.exempt_from_debounce:
                return trigger
            if self.triggers._debounced(now):
                self.trace.note("trigger_suppressed", trigger=trigger.name,
                                reason="inside 10-minute event debounce")
                continue
            return trigger
        return None

    def _pop_startup_trigger(self, state: str) -> Trigger | None:
        if self.startup_analysis_needed and state == "ACTIVE":
            self.startup_analysis_needed = False
            return Trigger("active_session_startup",
                           "service started during the active session; "
                           "reassess portfolio immediately",
                           exempt_from_debounce=True)
        return None

    @staticmethod
    def _expected_daily_moves(universe: dict) -> dict[str, float]:
        """Translate annualized ATM IV into the one-session move used by Tier 1."""
        out = {}
        for symbol, row in universe.items():
            iv = row.get("iv_atm")
            if iv is not None and float(iv) > 0:
                out[symbol] = float(iv) / math.sqrt(252)
        return out

    def _detect_assignment(self, positions: list[dict]) -> None:
        """An equity position is external to this options-only strategy.

        A new or changed underlying quantity is therefore an assignment/exercise
        (or manual intervention) and deserves an immediate reconciliation cycle.
        """
        current = {
            str(p.get("symbol")): float(p.get("qty") or 0)
            for p in positions
            if str(p.get("symbol")) in TRADED and float(p.get("qty") or 0) != 0
        }
        previous = getattr(self, "_last_position_quantities", None)
        self._last_position_quantities = current
        if previous is None:
            changed = current
            prefix = "underlying position present at startup"
        else:
            changed = {s: q for s, q in current.items() if previous.get(s) != q}
            changed.update({s: 0.0 for s in previous if s not in current})
            prefix = "underlying position changed"
        for symbol, qty in changed.items():
            self._queue_trigger(
                Trigger("assignment", f"{prefix}: {symbol} qty {qty:g}",
                        exempt_from_debounce=True),
                key=f"assignment-position:{symbol}:{qty:g}")

    def _dispatch(self, ns, fn, args, kwargs):
        if self.caps is None:
            raise RuntimeError("capabilities not built for this cycle")
        return self.caps.dispatch(ns, fn, args, kwargs)

    def _execution_control(self) -> dict:
        blocker_fn = getattr(self.executor, "entry_blockers", None)
        blockers = blocker_fn() if blocker_fn else []
        latest = getattr(self, "_latest_portfolio_snapshot", None) or {}
        scenario = latest.get("portfolio_scenario_risk") or {}
        scenario_breached = bool(
            scenario.get("status") == "ok" and scenario.get("breached"))
        return {
            "entries_frozen": bool(blockers),
            "risk_reducing_only": scenario_breached,
            "portfolio_scenario_risk": scenario,
            "latched": any(row.get("status") == "mismatch" for row in blockers),
            "blockers": [
                {"client_order_id": row.get("client_order_id"),
                 "purpose": row.get("purpose"),
                 "structure_id": row.get("structure_id"),
                 "status": row.get("status")}
                for row in blockers
            ],
        }

    def _sample_portfolio(self, now: dt.datetime, *, account: dict | None = None,
                          positions: list[dict] | None = None,
                          risk_state: dict | None = None,
                          record: bool = True, trace_record: bool = True) -> dict:
        """Join live broker marks and quotes to durable structures."""
        account = account or self.rest.account()
        positions = self.rest.positions() if positions is None else positions
        risk_state = risk_state or self.ledger.risk_snapshot(positions)
        symbols = list(dict.fromkeys(
            str(leg.get("symbol"))
            for structure in risk_state.get("structures") or []
            for leg in structure.get("legs") or [] if leg.get("symbol")))
        try:
            quotes = self.rest.option_quotes(symbols) if symbols else {}
        except Exception as exc:
            self.trace.error("portfolio_quotes", exc)
            quotes = {}
        underlyings = {str(row.get("underlying"))
                       for row in risk_state.get("structures") or []
                       if row.get("underlying")}
        spots = {}
        for symbol in underlyings:
            spot = self.series.last(symbol)
            if spot is None:
                try:
                    spot = float(self.rest.stock_latest_trade(symbol)["p"])
                except Exception:
                    continue
            spots[symbol] = float(spot)
        theses = {thesis.thesis_id: thesis for thesis in self.theses.list("open")}
        snap = portfolio.snapshot(account, risk_state, theses, quotes, spots, now,
                                  self.params)
        stress = portfolio_stress.stress_portfolio(
            risk_state.get("structures") or [], quotes, spots, now,
            horizon_days=getattr(
                self.params, "scenario_horizon_days", RISK.scenario_horizon_days),
            iv_shocks=(0.0, getattr(
                self.params, "scenario_iv_shock_pct",
                RISK.scenario_iv_shock_pct) / 100.0))
        scenario_limit_pct = getattr(
            self.params, "max_correlated_scenario_loss_pct",
            RISK.max_correlated_scenario_loss_pct)
        scenario_hysteresis_pct = getattr(
            self.params, "scenario_breach_hysteresis_pct",
            RISK.scenario_breach_hysteresis_pct)
        limit = float(account.get("equity") or 0) \
            * scenario_limit_pct / 100.0
        if stress.get("status") == "ok":
            worst = float((stress.get("worst_current") or {}).get(
                "current_book_pnl") or 0)
            loss = max(-worst, 0.0)
            snap["portfolio_scenario_risk"] = {
                "status": "ok", "worst_pnl": round(worst, 2),
                "loss_dollars": round(loss, 2),
                "loss_pct_of_equity": round(
                    loss / float(account.get("equity") or 1) * 100, 6),
                "limit_dollars": round(limit, 2),
                "limit_pct_of_equity": scenario_limit_pct,
                "clear_below_dollars": round(max(
                    limit - float(account.get("equity") or 0)
                    * scenario_hysteresis_pct / 100.0, 0), 2),
                "breached": loss > limit + 1e-9,
                "binding_scenario": stress.get("worst_current"),
                "sigma_by_underlying": stress.get("sigma_by_underlying"),
                "provenance": stress.get("provenance"),
            }
        else:
            snap["portfolio_scenario_risk"] = {
                "status": "incomplete", "breached": None,
                "missing_symbols": stress.get("missing_symbols") or [],
                "limit_dollars": round(limit, 2),
                "limit_pct_of_equity": scenario_limit_pct,
                "provenance": stress.get("provenance"),
            }
        policy_store = getattr(self, "exit_policies", None)
        if policy_store is not None:
            for row in snap.get("structures") or []:
                row["adaptive_exit_policy"] = policy_store.view(
                    str(row.get("structure_id")))
        trigger_store = getattr(self, "action_triggers", None)
        snap["action_triggers"] = trigger_store.observable(now.astimezone(
            dt.timezone.utc)) if trigger_store is not None else []
        snap["starting_equity"] = getattr(self, "starting_equity", None)
        if record:
            if not hasattr(self, "portfolio_history"):
                self.portfolio_history = deque(maxlen=180)
            lock = getattr(self, "_portfolio_lock", None)
            if lock is None:
                self._portfolio_lock = lock = threading.RLock()
            with lock:
                self.portfolio_history.append(snap)
                self._latest_portfolio_snapshot = snap
        if trace_record:
            self.trace.portfolio(snap)
        return snap

    def _portfolio_history_rows(self) -> list[dict]:
        lock = getattr(self, "_portfolio_lock", None)
        if lock is None:
            return list(getattr(self, "portfolio_history", ()))
        with lock:
            return list(getattr(self, "portfolio_history", ()))

    def _recent_portfolio_snapshot(self, now: dt.datetime,
                                   max_age_s: float = 20.0) -> dict | None:
        lock = getattr(self, "_portfolio_lock", None)
        with lock or threading.RLock():
            snap = getattr(self, "_latest_portfolio_snapshot", None)
            if not snap:
                return None
            try:
                observed = dt.datetime.fromisoformat(str(snap["observed_at"]))
                if (now.astimezone(dt.timezone.utc) - observed.astimezone(
                        dt.timezone.utc)).total_seconds() <= max_age_s:
                    return snap
            except (KeyError, TypeError, ValueError):
                pass
        return None

    async def _portfolio_monitor(self, tick_seconds: float = 10.0) -> None:
        """Read-only sampler that remains alive while Tier 2 is reasoning."""
        while True:
            now = dt.datetime.now(ET)
            trading_day = getattr(self, "_current_trading_day", now.weekday() < 5)
            if session_state(now, trading_day) != "CLOSED":
                try:
                    snap = await asyncio.to_thread(self._sample_portfolio, now)
                    await asyncio.to_thread(
                        self._evaluate_snapshot_exits, snap, now,
                        observe_adaptive=True)
                except Exception as exc:
                    self.trace.error("portfolio_monitor", exc)
            await asyncio.sleep(tick_seconds)

    def _record_trigger_evaluation(self, trigger: dict, status: str,
                                   now: dt.datetime, *, value: float | None = None,
                                   reason: str = "",
                                   gate_failures: list[str] | None = None) -> bool:
        """Persist one labelled evaluation transition without one-second log spam."""
        current = self.action_triggers.current().get(
            str(trigger["trigger_id"]), trigger)
        last_at = current.get("last_evaluated_at")
        if last_at:
            try:
                age = (now.astimezone(dt.timezone.utc) - dt.datetime.fromisoformat(
                    str(last_at))).total_seconds()
                same_value = (value is None and current.get("last_observed_value") is None)
                if value is not None and current.get("last_observed_value") is not None:
                    same_value = abs(float(current["last_observed_value"]) - value) < 0.01
                if (age < 5 and same_value
                        and current.get("last_evaluation_status") == status
                        and str(current.get("last_evaluation_reason") or "") == str(reason)):
                    return False
            except (TypeError, ValueError):
                pass
        fields = {
            "last_evaluation_status": str(status),
            "last_evaluation_reason": str(reason),
            "last_gate_failures": list(gate_failures or []),
            "last_evaluated_at": now.astimezone(dt.timezone.utc).isoformat(),
        }
        if value is not None:
            fields["last_observed_value"] = round(float(value), 4)
            fields["last_observed_at"] = fields["last_evaluated_at"]
        self.action_triggers.state(trigger["trigger_id"], "active", **fields)
        return True

    @staticmethod
    def _action_trigger_result_summary(out: dict) -> dict:
        keys = ("status", "reason", "failed_gates", "failed_gate_details",
                "order_id", "client_order_id",
                "qty", "limit_price", "max_loss", "executable_profit",
                "min_executable_profit")
        summary = {key: out.get(key) for key in keys if out.get(key) is not None}
        sizing = out.get("sizing") or {}
        if sizing:
            summary["binding_constraint"] = sizing.get("binding_constraint")
            summary["allowed_qty"] = sizing.get("allowed_qty")
        return summary

    def _blocked_trigger_escalations_this_session(self,
                                                   now: dt.datetime) -> int:
        """Count durable escalation grants for the current ET trading day."""
        count = 0
        for row in self.action_triggers.current().values():
            if (row.get("status") != "blocked_risk"
                    or not row.get("escalation_queued")):
                continue
            try:
                stamped = dt.datetime.fromisoformat(str(row["ts"])).astimezone(ET)
            except (KeyError, TypeError, ValueError):
                continue
            if stamped.date() == now.astimezone(ET).date():
                count += 1
        return count

    @staticmethod
    def _trigger_data_retry_due(trigger: dict, now: dt.datetime) -> bool:
        """Bound repeated full admission checks after a transient data failure."""
        if trigger.get("last_evaluation_status") != "waiting_data":
            return True
        try:
            last = dt.datetime.fromisoformat(str(trigger["last_evaluated_at"]))
            age = (now.astimezone(dt.timezone.utc)
                   - last.astimezone(dt.timezone.utc)).total_seconds()
            return age >= TRIGGER_DATA_RETRY_SECONDS
        except (KeyError, TypeError, ValueError):
            return True

    def _handle_action_trigger_result(self, trigger: dict, purpose: str,
                                      out: dict, now: dt.datetime) -> None:
        """Make every fire-time outcome durable and operationally distinct."""
        store = self.action_triggers
        trigger_id = str(trigger["trigger_id"])
        status = str(out.get("status") or "")
        summary = self._action_trigger_result_summary(out)
        if status in ("submitted", "submitted_close", "already_pending", "unknown"):
            if purpose == "entry" and status == "submitted":
                thesis_id = str((trigger.get("intent") or {}).get("thesis_id") or "")
                order_id = str(out.get("order_id") or "")
                thesis = self.theses.get(thesis_id) if thesis_id else None
                if thesis is not None and order_id and order_id not in thesis.order_ids:
                    self.theses.update(thesis_id, order_ids=[order_id])
            store.state(trigger_id, "fired", result=summary,
                        last_evaluation_status="fired",
                        last_evaluation_reason=str(out.get("reason") or status),
                        last_evaluated_at=now.astimezone(dt.timezone.utc).isoformat())
            self.trace.note("action_trigger_fired", trigger_id=trigger_id,
                            purpose=purpose, result=summary)
            self.trace.order({**out, "execution_path": "host_action_trigger",
                              "trigger_purpose": purpose})
            return
        if status == "blocked":
            failures = list(out.get("failed_gates") or [])
            reason = str(out.get("reason") or
                         "host admission gates refused the authorization at fire time")
            failure_set = set(failures)
            if failure_set and failure_set <= TRANSIENT_ACTION_TRIGGER_GATES:
                self._record_trigger_evaluation(
                    trigger, "waiting_data", now, reason=reason,
                    gate_failures=failures)
                self.trace.note("action_trigger_waiting_data",
                                trigger_id=trigger_id, purpose=purpose,
                                reason=reason, failed_gates=failures)
                return
            if not (failure_set & RISK_ACTION_TRIGGER_GATES):
                store.state(
                    trigger_id, "failed", result=summary,
                    last_evaluation_status="failed",
                    last_evaluation_reason=reason,
                    last_gate_failures=failures,
                    last_evaluated_at=now.astimezone(dt.timezone.utc).isoformat())
                self.trace.note("action_trigger_failed", trigger_id=trigger_id,
                                purpose=purpose, reason=reason,
                                failed_gates=failures, result=summary)
                return
            escalation_count = self._blocked_trigger_escalations_this_session(now)
            escalation_queued = (
                escalation_count < MAX_BLOCKED_TRIGGER_ESCALATIONS_PER_SESSION
                and self.triggers.cycles_this_session < MAX_CYCLES_PER_SESSION)
            suppressed_reason = ""
            if not escalation_queued:
                suppressed_reason = (
                    "session cycle cap reached"
                    if self.triggers.cycles_this_session >= MAX_CYCLES_PER_SESSION
                    else "blocked-trigger escalation cap reached")
            store.state(trigger_id, "blocked_risk", result=summary,
                        last_evaluation_status="blocked_risk",
                        last_evaluation_reason=reason,
                        last_gate_failures=failures,
                        escalation_queued=escalation_queued,
                        escalation_suppressed_reason=suppressed_reason,
                        last_evaluated_at=now.astimezone(dt.timezone.utc).isoformat())
            self.trace.note("action_trigger_blocked", trigger_id=trigger_id,
                            purpose=purpose, reason=reason,
                            failed_gates=failures, result=summary,
                            escalation_queued=escalation_queued,
                            escalation_suppressed_reason=suppressed_reason)
            if escalation_queued:
                self._queue_trigger(
                    Trigger("action_trigger_blocked",
                            f"host risk gates blocked {purpose} trigger {trigger_id}: "
                            f"{', '.join(failures) or reason}",
                            exempt_from_debounce=True),
                    key=f"action-trigger-blocked:{trigger_id}")
            return
        if status == "condition_not_met":
            value = out.get("executable_profit")
            self._record_trigger_evaluation(
                trigger, "waiting_price", now,
                value=float(value) if value is not None else None,
                reason=str(out.get("reason") or "fresh price no longer qualifies"))
            return
        if status in ("rejected", "mismatch", "failed", "proposed"):
            reason = str(out.get("reason") or status)
            store.state(trigger_id, "failed", result=summary,
                        last_evaluation_status="failed",
                        last_evaluation_reason=reason,
                        last_evaluated_at=now.astimezone(dt.timezone.utc).isoformat())
            self.trace.note("action_trigger_failed", trigger_id=trigger_id,
                            purpose=purpose, reason=reason, result=summary)
            return
        self._record_trigger_evaluation(
            trigger, status or "unknown", now,
            reason=str(out.get("reason") or "unclassified host outcome"))

    def _evaluate_action_triggers(self, now: dt.datetime) -> list[dict]:
        """Evaluate durable one-shot authorizations without a model round."""
        store = getattr(self, "action_triggers", None)
        if store is None:
            return []
        utc_now = now.astimezone(dt.timezone.utc)
        for trigger_id in store.expire_due(utc_now):
            self.trace.note("action_trigger_expired", trigger_id=trigger_id)
        rows = [row for row in store.current().values()
                if row.get("status") in ("active", "firing")
                and utc_now < dt.datetime.fromisoformat(str(row["expires_at"]))]
        results = []
        for trigger in rows:
            trigger_id = str(trigger["trigger_id"])
            purpose = str(trigger.get("purpose"))
            if not self._trigger_data_retry_due(trigger, now):
                continue
            try:
                if purpose == "exit":
                    latest = getattr(self, "_latest_portfolio_snapshot", None) or {}
                    structure = next((row for row in latest.get("structures") or []
                                      if str(row.get("structure_id")) ==
                                      str(trigger.get("structure_id"))), None)
                    if structure is None:
                        # A fill/reconciliation may be between snapshots. Do not
                        # mistake temporary invisibility for authorization to act.
                        self._record_trigger_evaluation(
                            trigger, "waiting_data", now,
                            reason="reconciled structure is not in the latest snapshot")
                        continue
                    threshold = float(trigger["condition"]["value"])
                    out = self.executor.close_structure(
                        structure,
                        reason=f"action trigger {trigger_id}: {trigger.get('reason')}",
                        now=now, min_executable_profit=threshold,
                        client_order_seed=trigger_id)
                    observed = out.get("executable_profit")
                elif purpose == "entry":
                    intent = intent_from_dict(trigger["intent"])
                    spot = self.series.last(intent.underlying)
                    if spot is None:
                        spot = float(self.rest.stock_latest_trade(intent.underlying)["p"])
                    drift_pct = abs(float(spot) / float(trigger["reference_spot"]) - 1) * 100
                    if drift_pct > float(trigger["max_spot_drift_pct"]):
                        out = {"status": "failed",
                               "reason": f"underlying drift {drift_pct:.3f}% exceeded authorization"}
                    else:
                        quotes = self.rest.option_quotes([leg.symbol for leg in intent.legs])
                        if any(leg.symbol not in quotes for leg in intent.legs):
                            self._record_trigger_evaluation(
                                trigger, "waiting_data", now,
                                reason="one or more executable leg quotes are unavailable")
                            continue
                        net = sum(
                            leg.sign * leg.ratio_qty * float(
                                quotes[leg.symbol]["ap" if leg.side == "buy" else "bp"])
                            for leg in intent.legs)
                        condition = trigger["condition"]
                        observed = net if condition["kind"] == "max_entry_debit" else -net
                        hit = (net > 0 and net <= float(condition["value"]) + 1e-9
                               if condition["kind"] == "max_entry_debit" else
                               net < 0 and -net + 1e-9 >= float(condition["value"]))
                        if not hit:
                            self._record_trigger_evaluation(
                                trigger, "waiting_price", now, value=observed,
                                reason="executable entry price has not reached the authorization")
                            continue
                        account = self.rest.account()
                        positions = self.rest.positions()
                        risk = self.ledger.risk_snapshot(positions)
                        snapshot = self._sample_portfolio(
                            now, account=account, positions=positions,
                            risk_state=risk, record=True, trace_record=True)
                        view = portfolio.with_trajectories(
                            snapshot, self._portfolio_history_rows())
                        underlyings = {intent.underlying.upper()} | {
                            str(row.get("underlying") or "").upper()
                            for row in view.get("structures") or []
                            if row.get("underlying")}
                        spots = {symbol: float(self.series.last(symbol) or
                                 self.rest.stock_latest_trade(symbol)["p"])
                                 for symbol in underlyings}
                        out = self.executor.execute_authorized(
                            intent, trigger_id=trigger_id,
                            economic_condition=condition,
                            authorization_deadline=dt.datetime.fromisoformat(
                                str(trigger["expires_at"])), now=utc_now,
                            equity=float(account["equity"]),
                            open_premium_at_risk=risk["premium_at_risk"],
                            realised_loss=risk["realised_loss"],
                            open_positions=view.get("structures") or [],
                            entry_evidence=trigger.get("evidence") or {},
                            market_spots=spots)
                else:
                    out = {"status": "failed", "reason": f"unknown purpose {purpose!r}"}
                self._handle_action_trigger_result(trigger, purpose, out, now)
                results.append(out)
            except Exception as exc:
                self.trace.error(f"action_trigger:{trigger_id}", exc)
        return results

    async def _action_trigger_monitor(self, tick_seconds: float = 1.0) -> None:
        """Fast path: dormant when no trigger exists; no model latency when armed."""
        while True:
            trigger_store = getattr(self, "action_triggers", None)
            has_unfinished = (trigger_store is not None and any(
                row.get("status") in ("active", "firing")
                for row in trigger_store.current().values()))
            if has_unfinished:
                now = dt.datetime.now(ET)
                if session_state(now, getattr(
                        self, "_current_trading_day", now.weekday() < 5)) != "CLOSED":
                    await asyncio.to_thread(self._evaluate_action_triggers, now)
            await asyncio.sleep(tick_seconds)

    def _live_trigger_universe(self, now: dt.datetime) -> dict:
        base = self._trigger_universe_cache or (
            (self.previous_bundle or {}).get("universe") or {})
        refresh_iv = (self._last_trigger_iv_refresh is None or
                      (now - self._last_trigger_iv_refresh).total_seconds()
                      >= TRIGGER_IV_REFRESH_SECONDS)
        try:
            current = preflight.live_trigger_universe(
                self.rest, self.series, base, self.expiries, now,
                refresh_iv=refresh_iv)
            self._trigger_universe_cache = current
            if refresh_iv:
                self._last_trigger_iv_refresh = now
            return current
        except Exception as exc:
            self.trace.error("live_trigger_universe", exc)
            return base

    # ---- setup ------------------------------------------------------------
    def _check_corporate_actions(self) -> None:
        """One call at startup. A dividend ex-date inside the window means an
        in-the-money short call can be assigned early to capture it."""
        from agent.config import WINDOW_CLOSE, WINDOW_OPEN
        try:
            actions = self.rest.corporate_actions(
                TRADED + MEGACAPS, dt.datetime.now(ET).date().isoformat(),
                (WINDOW_CLOSE.date() + dt.timedelta(days=3)).isoformat())
        except Exception as exc:
            self.trace.error("corporate_actions", exc)
            return
        inside = []
        for kind, rows in (actions or {}).items():
            for a in rows:
                ex = str(a.get("ex_date") or a.get("effective_date") or "")
                if WINDOW_OPEN.date().isoformat() <= ex <= WINDOW_CLOSE.date().isoformat():
                    inside.append({"kind": kind, "ex_date": ex,
                                   "symbol": a.get("symbol") or a.get("initiating_symbol"),
                                   "rate": a.get("rate") or a.get("new_rate")})
        self.trace.note("corporate_actions", total=sum(len(v) for v in (actions or {}).values()),
                        inside_window=inside)
        for a in inside:
            print(f"!! corporate action inside the window: {a['symbol']} {a['kind']} "
                  f"ex={a['ex_date']} -- in-the-money short calls can be assigned early")

    def _pick_expiries(self) -> list[str]:
        today = dt.datetime.now(ET).date()
        # Alpaca accepts an omitted upper bound and paginates the complete active
        # catalogue.  The session cache makes this a once-per-process discovery.
        rows = self.rest.contracts("SPY", today.isoformat(), None)
        return _eligible_expiries(rows, today)

    def _option_window(self, width: float = 7.0) -> list[str]:
        syms: list[str] = []
        for u in ("SPY", "QQQ"):
            spot = self.series.last(u) or float(self.rest.stock_latest_trade(u)["p"])
            # The observation bundle exposes every listed expiry because later
            # contracts still contribute marked equity.
            # Keep the websocket window on the nearest two; later expiries are
            # quoted on demand without exhausting the 200-symbol stream allowance.
            for exp in self.expiries[:2]:
                near = [c for c in self.rest.contracts(u, exp, exp)
                        if abs(float(c["strike_price"]) - spot) <= width]
                syms += [c["symbol"] for c in near]
        held = [p["symbol"] for p in self.rest.positions()]
        return list(dict.fromkeys(held + syms))[:200]     # held legs are never evicted

    async def start(self) -> None:
        if telemetry.setup():
            self.trace.note("telemetry", endpoint=os.environ.get("COLLECTOR_HOST"),
                            service=telemetry.SERVICE_NAME,
                            capture_content=telemetry.capture_content())
        account = self.rest.account()
        self._capture_starting_equity(account)
        legacy_alerts = self.executor.scan_prefixed_open_orders()
        if legacy_alerts:
            self.trace.note("untracked_prefixed_orders", orders=legacy_alerts)
        self._reconcile_execution()
        blockers = self.executor.entry_blockers()
        if blockers:
            self.trace.note(
                "entry_freeze", latched=any(b.get("status") == "mismatch" for b in blockers),
                blockers=[{"client_order_id": b.get("client_order_id"),
                           "purpose": b.get("purpose"), "status": b.get("status")}
                          for b in blockers])
        self.expiries = self._pick_expiries()
        self._check_corporate_actions()
        self.streams = StreamSet(self.prof, Handlers(
            on_equity_quote=self._on_equity, on_option_quote=self._on_option,
            on_news=self._on_news, on_trade_update=self._on_trade_update,
            on_error=self._on_error))
        await self.streams.start(TRADED + MEGACAPS + ["VXX"], self._option_window())
        self.trace.note("started", profile=self.profile_name, mode=self.mode,
                        execution_transport=self.rest.execution_transport,
                        robust_risk_pct=self.robust_risk_pct,
                        scenario_risk_pct=self.scenario_risk_pct,
                        model=f"{self.provider.spec.provider}/{self.provider.spec.model}",
                        expiries=self.expiries, streams=self.streams.status())

    # ---- Tier 2 -----------------------------------------------------------
    async def cycle(self, trigger: Trigger) -> str:
        async with self._cycle_lock:
            return await asyncio.to_thread(self._cycle_blocking, trigger)

    def _cycle_blocking(self, trigger: Trigger) -> str:
        with telemetry.invoke_agent(telemetry.SERVICE_NAME,
                                    trigger=trigger.name,
                                    cycle_id=self.trace.cycle_id or "") as agent_span:
            try:
                return self._cycle_inner(trigger)
            except Exception as exc:
                telemetry.record_error(agent_span, exc)
                raise
            finally:
                self.executor.end_cycle()

    def _cycle_inner(self, trigger: Trigger) -> str:
        cycle_started = time.monotonic()
        open_theses_before = {t.thesis_id for t in self.theses.list("open")}
        account = self.rest.account()
        equity = float(account["equity"])
        self._reconcile_execution()
        positions = self.rest.positions()
        risk_state = self.ledger.risk_snapshot(positions)
        portfolio_snapshot = self._sample_portfolio(
            dt.datetime.now(ET), account=account, positions=positions,
            risk_state=risk_state, record=True, trace_record=False)
        portfolio_view = portfolio.with_trajectories(
            portfolio_snapshot, self._portfolio_history_rows())
        trading_day = self._trading_day()
        bundle = preflight.build(self.rest, self.series, self.theses,
                                 trigger=trigger.as_dict(), universe=["SPY", "QQQ", "IWM"],
                                 expiries=self.expiries, account=account,
                                 previous=self.previous_bundle, trading_day=trading_day,
                                 history=self.history, blocked=self.blocked,
                                 execution_control=self._execution_control(),
                                 starting_equity=getattr(self, "starting_equity", None),
                                 runtime_state_age_seconds=getattr(
                                     self, "runtime_state_age_seconds", None),
                                 active_thesis_ids={str(s.get("thesis_id"))
                                                    for s in risk_state["structures"]
                                                    if s.get("thesis_id")},
                                 positions=positions, portfolio=portfolio_view)
        self.trace.start_cycle(trigger.as_dict(), bundle["bundle_hash"])
        self.executor.begin_cycle(self.trace.cycle_id or "unknown")
        self.trace.portfolio(portfolio_snapshot)
        self.trace.preflight(bundle)
        self.trace.reconcile(equity, risk_state["structures"], risk_state["realised_loss"])

        self.caps = Capabilities(self.rest, self.series, self.theses, self.executor,
                                 self.params, equity=equity,
                                 open_positions=portfolio_view["structures"],
                                 realised_loss=risk_state["realised_loss"],
                                 open_premium_at_risk=risk_state["premium_at_risk"],
                                 trigger=trigger.as_dict(),
                                 exit_policies=getattr(self, "exit_policies", None),
                                 action_triggers=getattr(self, "action_triggers", None),
                                 scheduled_events=bundle.get("scheduled_events"),
                                 current_scenario_breached=bool(
                                     (portfolio_view.get("portfolio_scenario_risk") or {})
                                     .get("breached")))
        self.sandbox.reset()

        messages = [{"role": "user", "content": prompt.payload(bundle)}]
        staged_checklist = None
        last_code_sha = None
        last_stderr = ""
        outcome = "NO_TRADE"
        reason = "no program produced a decision"
        robust_risk_pct = getattr(
            self, "robust_risk_pct", prompt.DEFAULT_ROBUST_RISK_PCT)
        scenario_risk_pct = getattr(
            self, "scenario_risk_pct", prompt.DEFAULT_SCENARIO_RISK_PCT)

        for rnd in range(1, MAX_ROUNDS + 1):
            sys_prompt = prompt.system_blocks(
                include_pretrade=bool(staged_checklist),
                robust_risk_pct=robust_risk_pct,
                scenario_risk_pct=scenario_risk_pct)
            spec = self.provider.spec
            request_messages = list(messages)
            if rnd > 1:
                # Make current state part of this request only.  Prepend it to the
                # latest observation/repair so the action instruction stays last,
                # while older manifests never enter conversation history.
                latest = dict(request_messages[-1])
                latest["content"] = (
                    prompt.state_turn(getattr(self.sandbox, "state_manifest", None))
                    + "\n\n" + latest["content"])
                request_messages[-1] = latest
            with telemetry.chat(spec.model, spec.provider, round_no=rnd) as chat_span:
                c = self.provider.complete(sys_prompt, request_messages)
                call_id = telemetry.new_call_id()
                telemetry.finish_chat(
                    chat_span, model=c.model, input_tokens=c.input_tokens,
                    output_tokens=c.output_tokens, reasoning_tokens=c.reasoning_tokens,
                    input_messages=[telemetry.user_message(
                        "\n\n".join(b["text"] for b in sys_prompt))]
                    + [telemetry.user_message(m["content"]) if m["role"] == "user"
                       else {"role": "assistant",
                             "parts": [{"type": "text", "content": m["content"]}]}
                       for m in request_messages],
                    output_messages=[telemetry.assistant_program_message(
                        c.thought, c.code, call_id, c.reasoning)] if not c.error else None)
                if c.error:
                    telemetry.record_error(chat_span, ValueError(c.error))
            if getattr(c, "fallbacks", None):
                self.trace.note("provider_fallback", answered_by=f"{c.provider}/{c.model}",
                                skipped=c.fallbacks)
                for f in c.fallbacks:
                    print(f"[providers] skipped {f}")
            if c.error:
                detail = c.error
                if getattr(c, "fallbacks", None) and len(c.fallbacks) > 1:
                    detail += "\n\ntried:\n" + "\n".join("  " + f for f in c.fallbacks)
                # one record, carrying the whole trail
                self.trace.error("providers", RuntimeError(detail))
                outcome, reason = "ERROR", "no provider produced a runnable program"
                break
            self.trace.program(rnd, c.thought, c.code, c.provider, c.model,
                               prompt.prompt_version(
                                   include_pretrade=bool(staged_checklist),
                                   robust_risk_pct=robust_risk_pct,
                                   scenario_risk_pct=scenario_risk_pct),
                               {"input": c.input_tokens, "output": c.output_tokens,
                                "cached": c.cached_tokens,
                                "cache_write": c.cache_write_tokens,
                                "reasoning": c.reasoning_tokens}, c.latency_s)
            # A staged order may only be confirmed by a later model program.  Mark
            # the boundary on the host before serving this program's RPC calls.
            self.executor.begin_program(rnd)
            self.caps.begin_program()
            code_sha = hashlib.sha256(c.code.encode()).hexdigest()[:16]
            if code_sha == last_code_sha:
                # Running it again produces the same traceback and burns the round.
                self.trace.note("identical_program", round=rnd, code_sha=code_sha)
                messages = [{"role": "user", "content": (
                    prompt.payload(bundle) + "\n\n" + prompt.repeat_turn(
                        last_stderr, hint_for(last_stderr),
                        _failing_line(c.code, last_stderr)))}]
                continue
            last_code_sha = code_sha

            # the program the model decided to run; carries the chat's call id so the
            # collector can join this execution back to the reasoning that caused it
            with telemetry.execute_tool("run_program", call_id,
                                        arguments={"code": c.code}) as prog_span:
                r = self.sandbox.run(c.code, bundle)
                if not r.ok:
                    telemetry.record_error(prog_span, RuntimeError(
                        r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "failed"))
            self.trace.evidence(r.stdout, r.calls, r.ok, r.duration_s, r.stderr,
                                r.state_manifest)
            last_stderr = r.stderr
            messages_tool_result = telemetry.tool_response_message(
                call_id, {"ok": r.ok, "stdout": r.stdout[-2000:], "calls": len(r.calls)})

            staged = self._latest_staged()
            program_decision = self.caps.program_decision
            trading_result = self.caps.trading_result
            if (trading_result or {}).get("status") in (
                    "submitted", "submitted_close", "unknown", "rejected",
                    "mismatch", "already_pending"):
                # Model-path orders used to be visible only indirectly through a
                # later FILL.  Record the broker-facing result at the decision
                # boundary so the proof trace has a complete submission chain.
                self.trace.order({**trading_result,
                                  "execution_path": "agent_program"})
            # A submission remains authoritative even if model code crashes after
            # the execute call.  A merely staged draft from a failed program does
            # not: discard it so repair code cannot confirm an unreviewed remnant.
            attempt_status = (getattr(self.executor, "_attempt_status", {})
                              .get(staged.verified.nonce) if staged else None)
            if not r.ok and staged and attempt_status == "submitted":
                outcome, reason = "SUBMITTED", "order submitted before program failure"
                break
            if not r.ok and staged and attempt_status == "unknown":
                outcome, reason = "DEGRADED", (
                    "submission became ambiguous before program failure; reconciliation pending")
                break
            if not r.ok and staged:
                self.executor.discard_staged()
                staged = None
                staged_checklist = None
            if staged:
                staged_checklist = staged.checklist()
                self.trace.verification(staged_checklist, staged.passed)
                if not staged.passed:
                    v = staged.verified
                    self.blocked.append({
                        "at": dt.datetime.now(ET).isoformat(timespec="minutes"),
                        "structure": f"{v.intent.underlying} {v.intent.family} "
                                     + "/".join(f"{l.strike:.0f}{l.option_type[0]}"
                                                for l in v.intent.legs),
                        "failed": [g.name for g in staged.results if not g.passed]})

            if r.ok:
                outcome, reason = self._classify(
                    r.stdout, staged, program_decision, trading_result)
                if outcome not in ("NEEDS_REVIEW", "CONTINUE"):
                    break
                if rnd == MAX_ROUNDS:
                    if staged:
                        self.executor.discard_staged()
                    final_status = (trading_result or {}).get("status")
                    if final_status in (
                            "condition_not_met", "condition_expired"):
                        outcome, reason = (
                            "NO_TRADE",
                            "fresh-price authorization ended without qualifying; "
                            "no order submitted")
                    elif final_status in (
                            "needs_evidence", "needs_revision",
                            "needs_price_authorization", "staged",
                            "awaiting_confirmation", "restaged") or outcome == "NEEDS_REVIEW":
                        outcome, reason = (
                            "INCOMPLETE",
                            "round budget exhausted during a safe pre-submit "
                            f"protocol state: {reason}; no order submitted")
                    else:
                        outcome, reason = (
                            "ERROR", "round budget exhausted without a terminal decision")
                    break
                if outcome == "NEEDS_REVIEW":
                    v = staged.verified
                    state_age = time.monotonic() - cycle_started
                    if state_age >= CONFIRMATION_REFRESH_AFTER_S:
                        try:
                            refreshed_at = dt.datetime.now(ET)
                            fresh_account = self.rest.account()
                            fresh_positions = self.rest.positions()
                            fresh_risk = self.ledger.risk_snapshot(fresh_positions)
                            fresh_snapshot = self._sample_portfolio(
                                refreshed_at, account=fresh_account,
                                positions=fresh_positions, risk_state=fresh_risk,
                                record=True, trace_record=True)
                            fresh_view = portfolio.with_trajectories(
                                fresh_snapshot, self._portfolio_history_rows())
                            bundle = dict(bundle)
                            bundle["execution_control"] = self._execution_control()
                            bundle = preflight.refresh_for_confirmation(
                                self.rest, self.series, bundle,
                                account=fresh_account, positions=fresh_positions,
                                portfolio=fresh_view, expiries=self.expiries,
                                now=refreshed_at, trading_day=trading_day)
                            self.caps.equity = float(fresh_account["equity"])
                            self.caps.open_positions = fresh_view["structures"]
                            self.caps.realised_loss = fresh_risk["realised_loss"]
                            self.caps.open_premium_at_risk = fresh_risk["premium_at_risk"]
                            portfolio_snapshot = fresh_snapshot
                            self.trace.note(
                                "confirmation_state_refreshed",
                                previous_age_seconds=round(state_age, 2),
                                bundle_hash=bundle["bundle_hash"],
                                equity=fresh_snapshot["equity"],
                                structures=fresh_snapshot["structure_count"])
                        except Exception as exc:
                            # The executor still re-prices and re-runs hard gates,
                            # but a model must not silently believe the old packet
                            # is current.
                            self.trace.error("confirmation_state_refresh", exc)
                            bundle = dict(bundle)
                            bundle["confirmation_refresh_error"] = (
                                "Fresh observation unavailable; decline the entry "
                                "unless the host returns a safe restage result.")
                    staged_packet = {
                        "underlying": v.intent.underlying,
                        "family": v.intent.family,
                        "thesis_id": getattr(v.intent, "thesis_id", ""),
                        "risk_budget": getattr(v.intent, "risk_budget", None),
                        "legs": [{"symbol": leg.symbol,
                                  "ratio_qty": leg.ratio_qty,
                                  "side": leg.side,
                                  "position_intent": leg.position_intent,
                                  "strike": leg.strike,
                                  "option_type": leg.option_type,
                                  "expiry": leg.expiry.isoformat()}
                                 for leg in v.intent.legs],
                        "qty": getattr(v, "qty", None),
                        "limit_price": getattr(v, "limit_price", None),
                        "max_loss": getattr(v, "max_loss", None),
                        "max_profit": (None if getattr(v, "max_profit", None) == float("inf")
                                       else getattr(v, "max_profit", None)),
                        "economic_condition": getattr(staged, "economic_condition", None),
                        "authorization_deadline": (
                            staged.authorization_deadline.isoformat()
                            if getattr(staged, "authorization_deadline", None) else None),
                        "confirmation_call": (
                            staged.confirmation_call()
                            if hasattr(staged, "confirmation_call") else None),
                    }
                    thesis_get = getattr(self.theses, "get", None)
                    staged_thesis = (thesis_get(
                        str(getattr(v.intent, "thesis_id", "") or ""))
                        if thesis_get else None)
                    next_content = prompt.review_turn(
                        bundle, staged_packet,
                        self.caps.entry_evidence(v.intent), staged_checklist or "",
                        thesis=(staged_thesis.to_json() if staged_thesis else None))
                elif (trading_result or {}).get("status") == "needs_evidence":
                    next_content = prompt.missing_evidence_turn(
                        bundle, trading_result or {}, MAX_ROUNDS - rnd)
                elif (trading_result or {}).get("status") == "needs_revision":
                    next_content = prompt.revision_turn(
                        bundle, trading_result or {}, MAX_ROUNDS - rnd)
                else:
                    next_content = (prompt.payload(bundle) + "\n\n" +
                                    prompt.continuation_turn(
                                        r.stdout, MAX_ROUNDS - rnd))
                # Latest-only conversation state: do not let the proposal's own
                # persuasive thought/code become the confirmation evidence.
                messages = [{"role": "user", "content": next_content}]
                continue

            if rnd == MAX_ROUNDS:
                outcome, reason = "ERROR", "program failed after the round budget"
                break
            messages = [{"role": "user", "content": (
                prompt.payload(bundle) + "\n\n" + prompt.repair_turn(
                    r.stderr, hint_for(r.stderr), c.code,
                    rounds_remaining=MAX_ROUNDS - rnd))}]

        self.trace.outcome(outcome, reason)
        if outcome != "DEGRADED":
            trigger_store = getattr(self, "action_triggers", None)
            protected_theses = set()
            if trigger_store is not None:
                protected_theses = {
                    str((row.get("intent") or {}).get("thesis_id") or "")
                    for row in trigger_store.current().values()
                    if row.get("purpose") == "entry"
                    and row.get("status") in ("active", "firing")}
            for thesis in self.theses.list("open"):
                if (thesis.thesis_id not in open_theses_before
                        and not thesis.order_ids
                        and thesis.thesis_id not in protected_theses):
                    self.theses.close(
                        thesis.thesis_id,
                        reason=f"cycle ended {outcome.lower()} without a submitted entry")
        self.history.append({
            "at": dt.datetime.now(ET).isoformat(timespec="minutes"),
            "trigger": trigger.name, "outcome": outcome, "reason": reason[:160]})
        self.previous_bundle = bundle
        self._trigger_universe_cache = dict(bundle["universe"])
        self.triggers.record_cycle(
            dt.datetime.now(ET), bundle["universe"], trigger,
            portfolio_snapshot=portfolio_snapshot)
        self._checkpoint_runtime_state()
        return outcome

    def _trading_day(self) -> bool:
        """Alpaca's clock is the authority on whether the market trades today."""
        try:
            c = self.rest.clock()
            return bool(c.get("is_open")) or (
                str(c.get("next_open", ""))[:10] == dt.datetime.now(ET).date().isoformat())
        except Exception:
            return dt.datetime.now(ET).weekday() < 5

    def _latest_staged(self):
        return self.executor.latest_staged

    def _classify(self, stdout: str, staged,
                  program_decision: dict | None = None,
                  trading_result: dict | None = None) -> tuple[str, str]:
        trading_status = (trading_result or {}).get("status")
        if trading_status == "submitted":
            return "SUBMITTED", "order submitted; fill reconciliation pending"
        if trading_status == "submitted_close":
            return "SUBMITTED", "closing order submitted; position remains open until filled"
        if trading_status == "already_pending":
            return "SUBMITTED", "closing order already pending; fill reconciliation pending"
        if trading_status == "trigger_armed":
            return "EXECUTED", "host-watched action trigger armed"
        if trading_status == "unknown":
            return "DEGRADED", "submission response ambiguous; durable reconciliation pending"
        if trading_status in ("rejected", "mismatch"):
            return "DEGRADED", f"broker execution {trading_status}; new entries remain guarded"
        if trading_status == "needs_evidence":
            missing = (trading_result or {}).get("missing") or []
            return "CONTINUE", "pre-submit evidence missing: " + "; ".join(missing)
        if trading_status == "needs_revision":
            issues = (trading_result or {}).get("issues") or []
            return "CONTINUE", "pre-submit thesis revision required: " + "; ".join(issues)
        if trading_status == "needs_price_authorization":
            return "CONTINUE", "live entry needs an explicit fresh-price boundary"
        if trading_status in ("condition_not_met", "condition_expired"):
            return "CONTINUE", "fresh-price authorization did not qualify; do not chase"
        if program_decision and program_decision.get("status") == "no_trade":
            return "NO_TRADE", program_decision["reason"]
        if trading_status == "proposed":
            return "PROPOSED", "propose mode completed; no broker order submitted"
        if trading_status == "proposed_close":
            return "PROPOSED", "propose mode completed; no closing order submitted"
        if staged:
            return "NEEDS_REVIEW", (
                "staged and awaiting confirmation" if staged.passed else
                "staged draft failed host gates and needs revision or explicit no-trade")
        return "CONTINUE", "program completed without a terminal decision"

    def step_shadow(self, now: dt.datetime, may_enter: bool) -> None:
        """One shadow tick against the same live quotes the agent sees."""
        try:
            spot = self.series.last("SPY") or float(
                self.rest.stock_latest_trade("SPY")["p"])
            if self.caps is None:
                caps = Capabilities(self.rest, self.series, self.theses, self.executor,
                                    self.params, equity=0.0,
                                    exit_policies=getattr(self, "exit_policies", None))
            else:
                caps = self.caps
            # Shadow policies are a fixed near-term comparator, not the agent's
            # opportunity universe.  Do not make every shadow tick download and
            # quote the complete long-dated catalogue.
            shadow_last = self.expiries[min(len(self.expiries), 4) - 1]
            chain = caps.dispatch("options", "tradeable_chain",
                                  ["SPY", self.expiries[0], shadow_last],
                                  {"width": 8})
            quotes = self.rest.option_quotes(
                [c["symbol"] for c in chain]
                + [l.symbol for b in self.shadow.books.values()
                   for p in b.positions if p.open for l in p.legs])
            self.shadow.step(chain, spot, quotes, now, may_enter=may_enter)
            self.shadow.record(quotes, now)
        except Exception as exc:                      # never let shadow break the agent
            self.trace.error("shadow", exc)

    # ---- Tier 0 sweep -----------------------------------------------------
    def _reconcile_execution(self, cancel_after_s: float = 60.0) -> list[dict]:
        updates = self.executor.reconcile_orders(cancel_after_s=cancel_after_s)
        descs = self.ledger.descriptors()
        states = self.ledger.states()
        summaries = self.ledger.structure_summaries()
        for update in updates:
            if update.get("delta_filled_qty"):
                self.trace.fill(update)
        # Scan durable terminal state as well as fresh updates. This closes the
        # thesis even if the process died after the broker fill but before tracing it.
        for oid, desc in descs.items():
            if (desc.get("purpose") == "exit"
                    and states.get(oid, {}).get("status") == "filled"
                    and desc.get("thesis_id")):
                thesis = self.theses.get(desc["thesis_id"])
                if thesis is not None and thesis.status == "open":
                    pnl = summaries.get(desc["structure_id"], {}).get("realised_pnl")
                    self.theses.close(thesis.thesis_id,
                                      reason=desc.get("reason") or "exit filled",
                                      realised=pnl)
        # A thesis is not exposure.  If every associated entry order terminated
        # without a fill, close it so it cannot enter future prompt bundles as a
        # fictional hedge or position.
        unfilled_terminal = {"canceled", "cancelled", "expired", "rejected",
                             "not_found"}
        open_theses = (self.theses.list("open")
                       if hasattr(self.theses, "list") else [])
        exposed_thesis_ids = {
            str(row.get("thesis_id") or "") for row in summaries.values()
            if float(row.get("ledger_open_qty") or 0) > 0 and row.get("thesis_id")}
        trigger_store = getattr(self, "action_triggers", None)
        active_trigger_thesis_ids = set()
        if trigger_store is not None:
            active_trigger_thesis_ids = {
                str((row.get("intent") or {}).get("thesis_id") or "")
                for row in trigger_store.current().values()
                if row.get("purpose") == "entry"
                and row.get("status") in ("active", "firing")}
        for thesis in open_theses:
            if not thesis.order_ids:
                if (thesis.thesis_id in exposed_thesis_ids
                        or thesis.thesis_id in active_trigger_thesis_ids):
                    continue
                # Staging is intentionally in memory only.  After a crash or
                # restart, an orderless open thesis cannot represent broker
                # exposure and must not contaminate the next prompt.
                self.theses.close(
                    thesis.thesis_id,
                    reason="unsubmitted draft did not survive reconciliation")
                self.trace.note("thesis_reconciled_unsubmitted",
                                thesis_id=thesis.thesis_id)
                continue
            order_states = [states.get(order_id) for order_id in thesis.order_ids]
            if (all(state is not None for state in order_states)
                    and all(str(state.get("status")) in unfilled_terminal
                            and float(state.get("filled_qty") or 0) <= 0
                            for state in order_states)):
                self.theses.close(thesis.thesis_id,
                                  reason="all entry orders terminated unfilled")
                self.trace.note("thesis_reconciled_unfilled",
                                thesis_id=thesis.thesis_id,
                                order_ids=list(thesis.order_ids))
        blocker_fn = getattr(self.executor, "entry_blockers", None)
        blockers = blocker_fn() if blocker_fn else []
        signature = tuple(sorted((str(b.get("client_order_id")), str(b.get("status")))
                                 for b in blockers))
        if signature != getattr(self, "_last_execution_blockers", None):
            self._last_execution_blockers = signature
            self.trace.note(
                "execution_control", entries_frozen=bool(blockers),
                exits_enabled=True,
                latched=any(b.get("status") == "mismatch" for b in blockers),
                blockers=[{"client_order_id": b.get("client_order_id"),
                           "purpose": b.get("purpose"), "status": b.get("status")}
                          for b in blockers])
        return updates

    @staticmethod
    def _legacy_mandatory_exit(desc: dict) -> bool:
        """Recognize the pre-upgrade scenario-repair order already in the ledger."""
        reason = str(desc.get("reason") or "").lower()
        return (desc.get("purpose") == "exit" and (
            "scenario-limit repair" in reason
            or "breached correlated scenario limit" in reason))

    def _recover_mandatory_exit_intents(self, structures: list[dict]) -> None:
        """Repair ledger/thesis invariants before attempting another close."""
        if not hasattr(self.ledger, "active_exit_intents"):
            return
        by_sid = {str(row.get("structure_id")): row for row in structures}
        for structure in structures:
            thesis_id = str(structure.get("thesis_id") or "")
            thesis = self.theses.get(thesis_id) if thesis_id else None
            if thesis is not None and thesis.status != "open":
                self.theses.reopen(
                    thesis_id,
                    reason="broker reconciliation confirms the structure is still open")
                self.trace.note("thesis_reopened_for_exposure", thesis_id=thesis_id,
                                structure_id=structure.get("structure_id"))

        states = self.ledger.states()
        active = {str(row.get("structure_id"))
                  for row in self.ledger.active_exit_intents()}
        for oid, desc in self.ledger.descriptors().items():
            sid = str(desc.get("structure_id") or "")
            if sid not in by_sid or sid in active:
                continue
            state = states.get(oid, {})
            status = str(state.get("status") or "").lower()
            remaining = int(by_sid[sid].get("qty") or 0)
            if (remaining > 0 and status in TERMINAL_STATUSES
                    and float(state.get("filled_qty") or 0) < float(desc.get("qty") or 0)
                    and (bool(desc.get("must_fill"))
                         or self._legacy_mandatory_exit(desc))):
                intent = self.ledger.arm_exit_intent(
                    structure_id=sid,
                    thesis_id=str(desc.get("thesis_id") or ""),
                    reason=str(desc.get("reason") or "mandatory risk-reducing exit"),
                    source=("legacy_scenario_repair" if not desc.get("must_fill")
                            else "durable_order_recovery"),
                    legacy_order_id=oid)
                self.trace.note("mandatory_exit_recovered", structure_id=sid,
                                exit_intent_id=intent.get("exit_intent_id"),
                                prior_order_id=oid, prior_status=status,
                                remaining_qty=remaining)
                active.add(sid)

    def _retry_mandatory_exits(self, structures: list[dict],
                               now: dt.datetime) -> list[str]:
        """Fresh-price retries continue until broker reconciliation says flat."""
        if not hasattr(self.ledger, "active_exit_intents"):
            return []
        self._recover_mandatory_exit_intents(structures)
        by_sid = {str(row.get("structure_id")): row for row in structures}
        acted: list[str] = []
        for intent in self.ledger.active_exit_intents():
            sid = str(intent.get("structure_id"))
            structure = by_sid.get(sid)
            if structure is None or int(structure.get("qty") or 0) < 1:
                self.ledger.record_exit_intent_state(
                    sid, "filled_flat", completed_at=now.astimezone(
                        dt.timezone.utc).isoformat())
                thesis_id = str(intent.get("thesis_id") or "")
                thesis = self.theses.get(thesis_id) if thesis_id else None
                if thesis is not None and thesis.status == "open":
                    self.theses.close(thesis_id, reason=str(
                        intent.get("reason") or "mandatory exit filled"))
                self.trace.note("mandatory_exit_completed", structure_id=sid)
                continue
            if self.ledger.active_exit(sid) is not None:
                continue
            try:
                result = self.executor.close_structure(
                    structure, reason=str(intent.get("reason") or
                                          "mandatory risk-reducing exit"),
                    now=now, must_fill=True,
                    mandatory_source=str(intent.get("source") or
                                         "mandatory_exit_retry"))
                self.trace.note(
                    "mandatory_exit_retry", structure_id=sid,
                    attempt=(self.ledger.exit_intents().get(sid) or {}).get("attempts"),
                    result_status=result.get("status"),
                    order_id=result.get("order_id"), qty=result.get("qty"),
                    limit_price=result.get("limit_price"))
                if result.get("status") not in ("already_pending",):
                    self.trace.order({**result,
                                      "execution_path": "host_mandatory_exit_retry"})
                acted.append(f"{sid}: {result.get('status')}")
            except Exception as exc:
                self.trace.error("mandatory_exit_retry", exc)
                acted.append(f"{sid}: retry failed ({exc})")
        return acted

    def sweep_exits(self, now: dt.datetime | None = None) -> list[str]:
        now = now or dt.datetime.now(ET)
        self._reconcile_execution()
        positions = self.rest.positions()
        self._detect_assignment(positions)
        state = self.ledger.risk_snapshot(positions)
        mandatory = self._retry_mandatory_exits(state["structures"], now)
        current_ids = {str(row.get("structure_id")) for row in state["structures"]}
        snap = self._recent_portfolio_snapshot(now)
        sampled_ids = {str(row.get("structure_id")) for row in
                       (snap or {}).get("structures") or []}
        if snap is None or sampled_ids != current_ids:
            try:
                snap = self._sample_portfolio(
                    now, account=self.rest.account(), positions=positions,
                    risk_state=state, record=True, trace_record=True)
            except Exception as exc:
                # Quote/account failure may suppress a profit exit, but must never
                # suppress hard loss, deadline or expiring-book final-session exits.
                self.trace.note("exit_snapshot_fallback", error=str(exc)[:400])
                snap = {"structures": state["structures"]}
        return mandatory + self._evaluate_snapshot_exits(
            snap, now, observe_adaptive=False)

    def _evaluate_snapshot_exits(self, snapshot: dict, now: dt.datetime,
                                 *, observe_adaptive: bool) -> list[str]:
        """Evaluate and execute exits from one immutable market observation."""
        acted = []
        lock = getattr(self, "_exit_sweep_lock", None)
        if lock is None:
            self._exit_sweep_lock = lock = threading.RLock()
        with lock:
            # Close structures containing shorts first. The multi-leg order stays
            # atomic, and its internal legs are also ordered short-first by Executor.
            structures = sorted(
                snapshot.get("structures") or [],
                key=lambda row: not any(leg.get("side") == "sell"
                                        for leg in row.get("legs") or []))
            for structure in structures:
                sid = str(structure.get("structure_id"))
                thesis = self.theses.get(str(structure.get("thesis_id") or ""))
                due, why = position_exit_due(
                    structure, thesis, now, self.params,
                    require_executable_profit=True)
                if observe_adaptive and getattr(self, "exit_policies", None) is not None:
                    policy = self.exit_policies.observe(
                        sid, structure.get("executable_unrealized_pl"),
                        quotes_valid=(not structure.get("missing_exit_quotes")
                                      and structure.get("executable_unrealized_pl") is not None))
                    structure["adaptive_exit_policy"] = policy
                    if not due and policy and policy.get("triggered"):
                        due = True
                        why = ("adaptive executable-profit trail: "
                               f"${float(policy['executable_profit']):,.0f} <= "
                               f"${float(policy['current_trigger_profit']):,.0f} after "
                               f"${float(policy['high_water_profit']):,.0f} high-water")
                if not due:
                    continue
                try:
                    result = self.executor.close_structure(structure, reason=why, now=now)
                    if result.get("status") != "already_pending":
                        self.trace.note("exit_due", structure_id=sid, reason=why,
                                        source=("adaptive" if why.startswith("adaptive")
                                                else "hard"))
                        self.trace.order({**result,
                                          "execution_path": "host_exit_sweep",
                                          "exit_source": (
                                              "adaptive" if why.startswith("adaptive")
                                              else "hard")})
                        acted.append(f"{sid}: {result['status']} ({why})")
                except Exception as exc:
                    self.trace.error("close_structure", exc)
                    acted.append(f"{sid}: close failed ({exc})")
        return acted

    def _shadow_tick(self, now: dt.datetime, state: str, every_s: float = 600.0) -> None:
        last = getattr(self, "_last_shadow", None)
        if state == "CLOSED":
            return
        if last and (now - last).total_seconds() < every_s:
            return
        self._last_shadow = now
        allowed, _ = entries_allowed(now, self._trading_day())
        self.step_shadow(now, may_enter=allowed)

    def _health_check(self, every_s: float = 300.0) -> None:
        """Stream health on a slow cadence -- robustness is judged, so it is recorded."""
        now = dt.datetime.now(dt.timezone.utc)
        last = getattr(self, "_last_health", None)
        if last and (now - last).total_seconds() < every_s:
            return
        self._last_health = now
        if self.streams is None:
            return
        bad = self.streams.unhealthy()
        self.trace.note("stream_health", status=self.streams.status(), unhealthy=bad)
        if bad:
            print(f"[{now:%H:%M:%S}] stream health: {', '.join(bad)}")

    # ---- main loop --------------------------------------------------------
    async def run_forever(self, tick_seconds: float = 10.0) -> None:
        await self.start()
        # Read-only marking is independent of the expensive decision thread, so
        # the UI and the next trigger keep a dynamic picture while a model spends
        # minutes simulating or reviewing a staged order.
        self._portfolio_monitor_task = asyncio.create_task(
            self._portfolio_monitor(tick_seconds))
        self._action_trigger_task = asyncio.create_task(
            self._action_trigger_monitor(1.0))
        last_state = None
        startup_now = dt.datetime.now(ET)
        startup_trading_day = self._trading_day()
        self.startup_analysis_needed = (
            session_state(startup_now, startup_trading_day) == "ACTIVE")
        while True:
            now = dt.datetime.now(ET)
            trading_day = self._trading_day()
            self._current_trading_day = trading_day
            state = session_state(now, trading_day)
            if state != last_state:
                self.trace.note("session_state", state=state, at=now.isoformat())
                if last_state == "CLOSED" and state == "WARM_UP":
                    self.triggers.new_session()
                last_state = state

            if state != "CLOSED":
                self.sweep_exits()
                allowed, why = entries_allowed(now, trading_day)
                book = self.rest.positions()
                portfolio_state = self.ledger.risk_snapshot(book)
                structures = portfolio_state.get("structures") or []
                portfolio_snapshot = self._recent_portfolio_snapshot(now)
                current_ids = {str(row.get("structure_id")) for row in structures}
                sampled_ids = {str(row.get("structure_id")) for row in
                               (portfolio_snapshot or {}).get("structures") or []}
                if portfolio_snapshot is None or sampled_ids != current_ids:
                    account = self.rest.account()
                    portfolio_snapshot = self._sample_portfolio(
                        now, account=account, positions=book,
                        risk_state=portfolio_state, record=True, trace_record=True)
                # Routine build reviews are governed by the same correlated
                # scenario loss used by admission, not by premium-at-risk.  The
                # latter can be large for a hedged book or small for a highly
                # correlated one, so substituting it silently changes the policy.
                build_scenario = (
                    (portfolio_snapshot or {}).get("portfolio_scenario_risk") or {})
                portfolio_risk_pct = (
                    float(build_scenario.get("loss_pct_of_equity") or 0) / 100.0
                    if build_scenario.get("status") == "ok" else float("inf"))
                # This must be a live stream/quote view. Comparing the last
                # preflight bundle with itself made market triggers impossible.
                universe = self._live_trigger_universe(now)
                if (startup_trigger := self._pop_startup_trigger(state)) is not None:
                    trig = startup_trigger
                elif (event_trigger := self._pop_event_trigger(now)) is not None:
                    trig = event_trigger
                elif self.restart_rebaseline_needed and state == "ACTIVE":
                    trig = Trigger("restart_rebaseline",
                                   "runtime state was stale; rebuild a fresh decision baseline",
                                   exempt_from_debounce=True)
                    self.restart_rebaseline_needed = False
                else:
                    trig = self.triggers.evaluate(
                        now, universe, book,
                        expected_daily_move=self._expected_daily_moves(universe),
                        structure_count=len(structures),
                        portfolio_risk_pct=portfolio_risk_pct,
                        portfolio_snapshot=portfolio_snapshot,
                        trading_day=trading_day)
                # Defence in depth: no Tier-2 source (including startup and
                # restart-rebaseline paths) may exceed the session cycle budget.
                # Deterministic Tier-0 exits have already run above and remain
                # independent of model availability and cycle accounting.
                if (trig is not None
                        and self.triggers.cycles_this_session
                        >= MAX_CYCLES_PER_SESSION):
                    self.trace.note("trigger_suppressed", trigger=trig.name,
                                    reason="session cycle cap reached")
                    trig = None
                cycle_outside_entry_window = (
                    trig is not None and trig.name in
                    ("deployment_floor", "fill_update", "assignment",
                     "portfolio_scenario_breach"))
                if trig and (allowed or cycle_outside_entry_window):
                    outcome = await self.cycle(trig)
                    print(f"[{now:%H:%M:%S}] cycle {trig.name} -> {outcome}")
                elif trig:
                    self.trace.note("trigger_suppressed", trigger=trig.name, reason=why)
            self._health_check()
            self._shadow_tick(now, state)
            self.series.checkpoint(f"{self.run_dir}/series.json")
            self._checkpoint_runtime_state(now)
            await asyncio.sleep(tick_seconds)


def main() -> None:
    ap = argparse.ArgumentParser(description="Alpaca options code agent")
    ap.add_argument("--profile", required=True, choices=["competition", "dev"],
                    help="explicit; there is no default")
    ap.add_argument("--mode", default="propose", choices=["propose", "execute"])
    ap.add_argument("--dev-models", action="store_true",
                    help="cheap Nebius models for build-and-test iteration")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--run-dir", default=".run")
    ap.add_argument("--robust-risk-pct", type=float, default=0.04,
                    help="maximum-loss fraction for a three-model stable entry")
    ap.add_argument("--scenario-risk-pct", type=float, default=0.04,
                    help="maximum correlated executable scenario loss as an equity fraction")
    args = ap.parse_args()

    load_env()
    agent = Agent(args.profile, args.mode, args.dev_models, args.run_dir,
                  args.robust_risk_pct, args.scenario_risk_pct)
    if args.profile == "competition" and args.mode == "execute":
        print("!! competition account, execute mode -- orders will be real paper trades")

    if args.once:
        async def one():
            await agent.start()
            out = await agent.cycle(Trigger("manual", "single cycle via --once"))
            print(f"outcome: {out}")
            await agent.streams.stop()
            telemetry.shutdown()
        asyncio.run(one())
    else:
        asyncio.run(agent.run_forever())


if __name__ == "__main__":
    main()
