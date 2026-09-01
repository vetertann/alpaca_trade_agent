"""Thin structured adapter around Alpaca's official CLI.

The decision sandbox never invokes processes.  Only the credential-owning host uses
the CLI, and credentials are supplied through a private child environment rather
than command-line arguments or output.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent.config import Profile


class CLIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class AlpacaCLI:
    def __init__(self, profile: Profile, binary: str | None = None,
                 timeout_s: int = 30):
        requested = binary or os.environ.get("ALPACA_CLI_PATH") or "alpaca"
        resolved = shutil.which(requested) if not Path(requested).is_file() else requested
        if not resolved:
            raise FileNotFoundError(
                f"Alpaca CLI not found at {requested!r}; install it or set ALPACA_CLI_PATH")
        self.binary = str(resolved)
        self.profile = profile
        self.timeout_s = int(timeout_s)

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        # Never let interactive CLI diagnostics bleed headers or HTTP bodies into
        # the durable execution-error ledger.
        for name in ("ALPACA_PROFILE", "ALPACA_DEBUG", "ALPACA_VERBOSE", "ALPACA_TRACE"):
            env.pop(name, None)
        env.update({
            "ALPACA_API_KEY": self.profile.api_key,
            "ALPACA_SECRET_KEY": self.profile.secret_key,
            "ALPACA_LIVE_TRADE": "false",
            "ALPACA_OUTPUT": "json",
            "ALPACA_QUIET": "true",
        })
        return env

    @staticmethod
    def _decode(text: str) -> Any:
        value = text.strip()
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            # Some versions put a short warning before the JSON document.
            for line in reversed(value.splitlines()):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            return value

    @staticmethod
    def _status(payload: Any, text: str) -> int | None:
        if isinstance(payload, dict):
            for key in ("status", "status_code", "http_status"):
                value = payload.get(key)
                if isinstance(value, int) or str(value or "").isdigit():
                    return int(value)
            error = payload.get("error")
            if isinstance(error, dict):
                return AlpacaCLI._status(error, text)
        match = re.search(r"\b(4\d\d|5\d\d)\b", text)
        return int(match.group(1)) if match else None

    def request(self, method: str, path: str, *, body: dict | None = None,
                query: str | None = None) -> Any:
        command = [self.binary, "api", method.upper(), path, "--quiet",
                   "--timeout", str(self.timeout_s)]
        if query:
            command += ["--query", query]
        encoded = json.dumps(body, separators=(",", ":")) if body is not None else None
        try:
            proc = subprocess.run(
                command, input=encoded, text=True, capture_output=True,
                env=self._environment(), timeout=self.timeout_s + 10, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CLIError("Alpaca CLI timed out; broker outcome is ambiguous") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            payload = self._decode(proc.stderr or proc.stdout)
            raise CLIError(detail or f"Alpaca CLI exited {proc.returncode}",
                           status_code=self._status(payload, detail), payload=payload)
        return self._decode(proc.stdout)
