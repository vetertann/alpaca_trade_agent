"""Host side of the sandbox.

Spawns the child, serves capability calls over a socketpair, and enforces policy
on every one. Credentials never enter the child's environment -- that is the real
boundary, so the sandbox is not asked to do more than it can.
"""
from __future__ import annotations

import json
import os
import pickle
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent.sandbox.protocol import decode, encode, err_frame, ok_frame

SECRET_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "ANTHROPIC", "OPENAI",
                  "NEBIUS", "FEATHERLESS", "ALPACA")


def clean_environment() -> dict[str, str]:
    """Everything except credentials."""
    return {k: v for k, v in os.environ.items()
            if not any(m in k.upper() for m in SECRET_MARKERS)}


@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    calls: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    timed_out: bool = False

    @property
    def traceback(self) -> str:
        return self.stderr.strip()


HINTS = {
    "NameError": "The name is not defined. Capability namespaces available: "
                 "market, options, account, orders, vol, oi_gamma, risk, trading, "
                 "thesis, replay, learned. Do not rebind them.",
    "AttributeError": "That function does not exist on the namespace. Check the "
                      "capability list in the preamble rather than guessing.",
    "KeyError": "A key was missing from a result. Result shapes are exactly as "
                "documented; do not assume extra fields.",
    "TypeError": "Argument types or arity are wrong. Check the signature.",
    "ImportError": "Imports are limited to datetime, json, math, statistics, time, numpy, "
                   "pandas, scipy, and scipy.stats. These modules and the capability "
                   "namespaces are already preloaded; do not import OS, process, "
                   "filesystem, or network modules.",
    "RpcError": "The host refused or failed the capability call. The message says "
                 "which gate or error produced it.",
}


def hint_for(stderr: str) -> str:
    for kind, text in HINTS.items():
        if kind in stderr:
            return text
    return ""


class Sandbox:
    """One sandbox per decision cycle. The namespace persists across rounds."""

    def __init__(self, dispatch: Callable[[str, str, list, dict], object],
                 workdir: str | Path = ".run", timeout_s: float = 60.0):
        self.dispatch = dispatch
        self.timeout_s = timeout_s
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workdir / "sandbox_state.pkl"
        self.calls: list[dict] = []

    def reset(self) -> None:
        self.state_path.unlink(missing_ok=True)
        self.calls = []

    def _serve(self, sock: socket.socket, stop: threading.Event,
               otel_ctx=None) -> None:
        # OTel context is thread-local, so capability spans raised here would
        # otherwise detach from the program span that caused them.
        token = None
        if otel_ctx is not None:
            try:
                from opentelemetry import context as _octx
                token = _octx.attach(otel_ctx)
            except Exception:
                token = None
        buf = b""
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    req = decode(line)
                    t0 = time.monotonic()
                    value = self.dispatch(req["ns"], req["fn"],
                                          req.get("args", []), req.get("kwargs", {}))
                    self.calls.append({"ns": req["ns"], "fn": req["fn"],
                                       "ms": round((time.monotonic() - t0) * 1000, 1)})
                    reply = ok_frame(value)
                except Exception as exc:                       # noqa: BLE001
                    self.calls.append({"ns": req.get("ns"), "fn": req.get("fn"),
                                       "error": type(exc).__name__})
                    reply = err_frame(type(exc).__name__, str(exc)[:400])
                try:
                    sock.sendall(encode(reply))
                except OSError:
                    return
        if token is not None:
            try:
                from opentelemetry import context as _octx
                _octx.detach(token)
            except Exception:
                pass

    def run(self, code: str, obs: dict) -> RunResult:
        parent, child = socket.socketpair()
        stop = threading.Event()
        try:
            from opentelemetry import context as _octx
            otel_ctx = _octx.get_current()
        except Exception:
            otel_ctx = None
        server = threading.Thread(target=self._serve, args=(parent, stop, otel_ctx),
                                  daemon=True)
        server.start()
        payload = json.dumps({"code": code, "obs": obs,
                              "state_path": str(self.state_path)})
        t0 = time.monotonic()
        timed_out = False
        try:
            src = str(Path(__file__).resolve().parents[2])
            boot = (f"import sys; sys.path.insert(0, {src!r}); "
                    "from agent.sandbox.child import main; sys.exit(main())")
            env = clean_environment() | {"AGENT_RPC_FD": str(child.fileno())}
            os.set_inheritable(child.fileno(), True)
            # An empty working directory, because scrubbing credentials from the
            # environment achieves nothing while `.env` sits one relative path away.
            jail = self.workdir / "cwd"
            jail.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [sys.executable, "-c", boot],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(child.fileno(),), env=env, cwd=str(jail), text=True)
            try:
                out, err = proc.communicate(payload, timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                timed_out = True
            ok = proc.returncode == 0 and not timed_out
        finally:
            stop.set()
            server.join(timeout=2)
            parent.close()
            child.close()
        return RunResult(ok, out, err, list(self.calls),
                         round(time.monotonic() - t0, 2), timed_out)
