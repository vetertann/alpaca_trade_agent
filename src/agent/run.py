"""Entry point. Tier 0 runs continuously; Tier 2 runs when a predicate fires."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import datetime as dt
import os

from agent.brain import preflight, prompt, providers
from agent.brain.shadow import ShadowRunner
from agent.brain.loop import (MAX_CYCLES_PER_SESSION, Trigger, TriggerState,
                              entries_allowed, position_exit_due, session_state)
from agent.config import ET, load_env, profile
from agent.host.capabilities import Capabilities
from agent.host.execution import Executor
from agent.host.ledger import ExecutionLedger
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


class Agent:
    def __init__(self, profile_name: str, mode: str, dev_models: bool,
                 run_dir: str = ".run"):
        self.prof = profile(profile_name)
        self.profile_name = profile_name
        self.mode = mode
        self.rest = Rest(self.prof)
        self.series = RollingSeries()
        self.theses = ThesisStore(f"{run_dir}/theses.jsonl")
        self.trace = Trace(f"{run_dir}/trace.jsonl")
        self.params = RISK
        self.ledger = ExecutionLedger(f"{run_dir}/execution.jsonl")
        self.executor = Executor(self.rest, self.params, profile_name, mode=mode,
                                 ledger=self.ledger)
        self.triggers = TriggerState()
        self.sandbox = Sandbox(self._dispatch, workdir=run_dir, timeout_s=CYCLE_BUDGET_S)
        self.provider = providers.for_role("decision", dev=dev_models, max_tokens=8000)
        self.run_dir = run_dir
        self.previous_bundle: dict | None = None
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

    # ---- Tier 0 -----------------------------------------------------------
    def _on_equity(self, sym, bid, ask, when):
        if bid > 0 and ask > 0:
            self.series.observe(sym, (bid + ask) / 2, when)

    def _on_option(self, sym, bid, ask, when):
        if bid > 0 and ask > 0:
            self.series.observe(sym, (bid + ask) / 2, when)

    def _on_news(self, article):
        held = {t.thesis_id.split("_")[-2].upper() for t in self.theses.list("open")}
        syms = set(article.get("symbols") or [])
        if syms & (held | set(TRADED)):
            self.trace.note("news_position_keyed", headline=article.get("headline"),
                            symbols=sorted(syms))

    def _on_trade_update(self, data):
        order = data.get("order") or {}
        self.trace.note("trade_update", event=data.get("event"),
                        order_id=order.get("id"), status=order.get("status"))
        if order.get("id") in self.ledger.descriptors():
            try:
                state = self.ledger.record_state(order)
                if state.get("delta_filled_qty"):
                    self.trace.fill(state)
            except Exception as exc:
                self.trace.error("trade_update_reconcile", exc)

    def _on_error(self, feed, exc):
        self.trace.error(f"stream:{feed}", exc)

    def _dispatch(self, ns, fn, args, kwargs):
        if self.caps is None:
            raise RuntimeError("capabilities not built for this cycle")
        return self.caps.dispatch(ns, fn, args, kwargs)

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
        rows = self.rest.contracts("SPY", today.isoformat(),
                                   (today + dt.timedelta(days=8)).isoformat())
        return sorted({c["expiration_date"] for c in rows})[:2]

    def _option_window(self, width: float = 7.0) -> list[str]:
        syms: list[str] = []
        for u in ("SPY", "QQQ"):
            spot = self.series.last(u) or float(self.rest.stock_latest_trade(u)["p"])
            for exp in self.expiries:
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
        self.expiries = self._pick_expiries()
        self._check_corporate_actions()
        self.streams = StreamSet(self.prof, Handlers(
            on_equity_quote=self._on_equity, on_option_quote=self._on_option,
            on_news=self._on_news, on_trade_update=self._on_trade_update,
            on_error=self._on_error))
        await self.streams.start(TRADED + MEGACAPS + ["VXX"], self._option_window())
        self.trace.note("started", profile=self.profile_name, mode=self.mode,
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
        account = self.rest.account()
        equity = float(account["equity"])
        self._reconcile_execution()
        positions = self.rest.positions()
        risk_state = self.ledger.risk_snapshot(positions)
        trading_day = self._trading_day()
        bundle = preflight.build(self.rest, self.series, self.theses,
                                 trigger=trigger.as_dict(), universe=["SPY", "QQQ", "IWM"],
                                 expiries=self.expiries, account=account,
                                 previous=self.previous_bundle, trading_day=trading_day,
                                 history=self.history, blocked=self.blocked)
        self.trace.start_cycle(trigger.as_dict(), bundle["bundle_hash"])
        self.executor.begin_cycle(self.trace.cycle_id or "unknown")
        self.trace.preflight(bundle)
        self.trace.reconcile(equity, risk_state["structures"], risk_state["realised_loss"])

        self.caps = Capabilities(self.rest, self.series, self.theses, self.executor,
                                 self.params, equity=equity,
                                 open_positions=risk_state["structures"],
                                 realised_loss=risk_state["realised_loss"],
                                 open_premium_at_risk=risk_state["premium_at_risk"])
        self.sandbox.reset()

        messages = [{"role": "user", "content": prompt.payload(bundle)}]
        staged_checklist = None
        last_code_sha = None
        last_stderr = ""
        outcome = "NO_TRADE"
        reason = "no program produced a decision"

        for rnd in range(1, MAX_ROUNDS + 1):
            sys_prompt = prompt.system_blocks(include_pretrade=bool(staged_checklist))
            spec = self.provider.spec
            with telemetry.chat(spec.model, spec.provider, round_no=rnd) as chat_span:
                c = self.provider.complete(sys_prompt, messages)
                call_id = telemetry.new_call_id()
                telemetry.finish_chat(
                    chat_span, model=c.model, input_tokens=c.input_tokens,
                    output_tokens=c.output_tokens, reasoning_tokens=c.reasoning_tokens,
                    input_messages=[telemetry.user_message(
                        "\n\n".join(b["text"] for b in sys_prompt))]
                    + [telemetry.user_message(m["content"]) if m["role"] == "user"
                       else {"role": "assistant",
                             "parts": [{"type": "text", "content": m["content"]}]}
                       for m in messages],
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
                               prompt.prompt_version(include_pretrade=bool(staged_checklist)),
                               {"input": c.input_tokens, "output": c.output_tokens,
                                "cached": c.cached_tokens,
                                "cache_write": c.cache_write_tokens,
                                "reasoning": c.reasoning_tokens}, c.latency_s)
            # A staged order may only be confirmed by a later model program.  Mark
            # the boundary on the host before serving this program's RPC calls.
            self.executor.begin_program(rnd)
            code_sha = hashlib.sha256(c.code.encode()).hexdigest()[:16]
            if code_sha == last_code_sha:
                # Running it again produces the same traceback and burns the round.
                self.trace.note("identical_program", round=rnd, code_sha=code_sha)
                messages += [{"role": "assistant", "content": c.raw},
                             {"role": "user", "content": prompt.repeat_turn(
                                 last_stderr, hint_for(last_stderr),
                                 _failing_line(c.code, last_stderr))}]
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
            self.trace.evidence(r.stdout, r.calls, r.ok, r.duration_s, r.stderr)
            last_stderr = r.stderr
            messages_tool_result = telemetry.tool_response_message(
                call_id, {"ok": r.ok, "stdout": r.stdout[-2000:], "calls": len(r.calls)})

            staged = self._latest_staged()
            # A submission remains authoritative even if model code crashes after
            # the execute call.  A merely staged draft from a failed program does
            # not: discard it so repair code cannot confirm an unreviewed remnant.
            if not r.ok and staged and staged.verified.nonce in self.executor._consumed:
                outcome, reason = "EXECUTED", "order submitted before program failure"
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
                outcome, reason = self._classify(r.stdout, staged)
                if outcome != "NEEDS_CONFIRM":
                    break
                messages += [{"role": "assistant", "content": c.raw},
                             {"role": "user", "content": prompt.observation_turn(
                                 r.stdout, staged_checklist)}]
                continue

            if rnd == MAX_ROUNDS:
                outcome, reason = "ERROR", "program failed after the round budget"
                break
            messages += [{"role": "assistant", "content": c.raw},
                         {"role": "user", "content": prompt.repair_turn(
                             r.stderr, hint_for(r.stderr))}]

        self.trace.outcome(outcome, reason)
        self.history.append({
            "at": dt.datetime.now(ET).isoformat(timespec="minutes"),
            "trigger": trigger.name, "outcome": outcome, "reason": reason[:160]})
        self.previous_bundle = bundle
        self.triggers.record_cycle(dt.datetime.now(ET), bundle["universe"], trigger)
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

    def _classify(self, stdout: str, staged) -> tuple[str, str]:
        up = stdout.upper()
        if "SUBMITTED" in up or (staged and staged.verified.nonce in self.executor._consumed):
            return "EXECUTED", "order submitted"
        if staged and not staged.passed:
            failed = [g.name for g in staged.results if not g.passed]
            kind = ("BLOCKED_LIQUIDITY" if any(g in ("quote_valid", "spread")
                                               for g in failed) else "BLOCKED_RISK")
            return kind, f"gates failed: {', '.join(failed)}"
        if staged and staged.passed:
            return "NEEDS_CONFIRM", "staged and awaiting confirmation"
        if "NO_TRADE" in up:
            return "NO_TRADE", "declined, reason recorded in the program output"
        return "NO_TRADE", "no order staged"

    def step_shadow(self, now: dt.datetime, may_enter: bool) -> None:
        """One shadow tick against the same live quotes the agent sees."""
        try:
            spot = self.series.last("SPY") or float(
                self.rest.stock_latest_trade("SPY")["p"])
            if self.caps is None:
                caps = Capabilities(self.rest, self.series, self.theses, self.executor,
                                    self.params, equity=0.0)
            else:
                caps = self.caps
            chain = caps.dispatch("options", "tradeable_chain",
                                  ["SPY", self.expiries[0], self.expiries[-1]],
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
        return updates

    def sweep_exits(self, now: dt.datetime | None = None) -> list[str]:
        acted = []
        now = now or dt.datetime.now(ET)
        self._reconcile_execution()
        state = self.ledger.risk_snapshot(self.rest.positions())
        # When orphan legs exist, close shorts first so liquidation cannot create
        # uncovered short exposure by selling a hedge before buying the short back.
        structures = sorted(state["structures"],
                            key=lambda s: not any(l["side"] == "sell" for l in s["legs"]))
        for structure in structures:
            due, why = position_exit_due(structure, {}, now, self.params)
            if not due:
                continue
            self.trace.note("exit_due", structure_id=structure["structure_id"], reason=why)
            try:
                result = self.executor.close_structure(structure, reason=why, now=now)
                self.trace.order(result)
                acted.append(f"{structure['structure_id']}: {result['status']} ({why})")
            except Exception as exc:
                self.trace.error("close_structure", exc)
                acted.append(f"{structure['structure_id']}: close failed ({exc})")
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
        last_state = None
        while True:
            now = dt.datetime.now(ET)
            trading_day = self._trading_day()
            state = session_state(now, trading_day)
            if state != last_state:
                self.trace.note("session_state", state=state, at=now.isoformat())
                if last_state == "CLOSED" and state == "WARM_UP":
                    self.triggers.new_session()
                last_state = state

            if state != "CLOSED":
                self.sweep_exits()
                allowed, why = entries_allowed(now, trading_day)
                trig = self.triggers.evaluate(
                    now, self.previous_bundle["universe"] if self.previous_bundle else {},
                    self.rest.positions(), trading_day=trading_day)
                if trig and (allowed or trig.name in ("deployment_floor",)):
                    outcome = await self.cycle(trig)
                    print(f"[{now:%H:%M:%S}] cycle {trig.name} -> {outcome}")
                elif trig:
                    self.trace.note("trigger_suppressed", trigger=trig.name, reason=why)
            self._health_check()
            self._shadow_tick(now, state)
            self.series.checkpoint(f"{self.run_dir}/series.json")
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
    args = ap.parse_args()

    load_env()
    agent = Agent(args.profile, args.mode, args.dev_models, args.run_dir)
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
