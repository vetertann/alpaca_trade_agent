"""Runs inside the sandbox process.

Holds no credentials and opens no sockets. Capability namespaces are thin stubs
that block on a pipe to the host, which owns the keys and runs the verifier.
"""
from __future__ import annotations

import builtins
import json
import os
import sys
import traceback
from types import ModuleType

from agent.sandbox.protocol import RpcError, call_frame, decode, encode

_RPC_FD = int(os.environ.get("AGENT_RPC_FD", "3"))
_rpc_in = os.fdopen(_RPC_FD, "rb", 0)
_rpc_out = os.fdopen(os.dup(_RPC_FD), "wb", 0)


def _invoke(ns: str, fn: str, args, kwargs):
    _rpc_out.write(encode(call_frame(ns, fn, list(args), dict(kwargs))))
    line = _rpc_in.readline()
    if not line:
        raise RpcError("host closed the capability channel")
    reply = decode(line)
    if not reply.get("ok"):
        raise RpcError(f"{reply.get('error')}: {reply.get('message')}")
    return reply["value"]


class _Namespace:
    """Read-only binding. Attribute access produces a bound capability call."""

    __slots__ = ("_name",)

    def __init__(self, name: str):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, fn: str):
        if fn.startswith("_"):
            raise AttributeError(fn)
        ns = object.__getattribute__(self, "_name")

        def bound(*args, **kwargs):
            return _invoke(ns, fn, args, kwargs)

        bound.__name__ = f"{ns}.{fn}"
        return bound

    def __setattr__(self, *_):
        raise AttributeError("capability namespaces are read-only")

    def __repr__(self):
        return f"<capability {object.__getattribute__(self, '_name')}>"


PROGRAM_FILENAME = "<program>"

NAMESPACES = ("market", "options", "account", "orders", "vol", "oi_gamma",
              "risk", "trading", "thesis", "replay", "learned")


def _restricted_imports(modules: dict[str, ModuleType]):
    """Return an importer limited to modules deliberately exposed to programs.

    The child is deployed on a dedicated VM, so this is a reliability boundary,
    not a security sandbox. It prevents generated programs from reaching for
    unrelated process, filesystem, or networking modules and gives the model a
    stable runtime contract.

    Only imports written in the generated program itself are checked. Library
    internals are not: numpy importing ``numpy._core._methods`` and pandas
    reaching for ``dateutil`` are not the model's decisions, and blocking them
    breaks the very modules the allowlist exists to provide.
    """
    import importlib
    import sys as _sys

    roots = sorted({name.split(".")[0] for name in modules})
    allowed = ", ".join(roots)
    real_import = builtins.__import__

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        frame = _sys._getframe(1)
        if frame.f_code.co_filename != PROGRAM_FILENAME:
            return real_import(name, globals, locals, fromlist, level)
        if level != 0:
            raise ImportError("relative imports are not available in the "
                              "decision runtime")
        if name.split(".")[0] not in roots:
            raise ImportError(
                f"import {name!r} is not available in the decision runtime; "
                f"allowed imports: {allowed}. The capability namespaces and "
                "scientific modules described in the prompt are already preloaded.")
        module = importlib.import_module(name)
        # `import a.b` binds `a`; `from a.b import c` binds the submodule.
        return module if fromlist else _sys.modules[name.split(".")[0]]

    return restricted_import


class Obs(dict):
    """The bundle. Supports obs["universe"] and obs.universe alike -- the prompt
    teaches attribute access and a plain dict would fail a whole round on it."""

    def __getattr__(self, name):
        try:
            return _wrap(dict.__getitem__(self, name))
        except KeyError:
            raise AttributeError(
                f"obs has no field {name!r}. Available: {sorted(self)}") from None

    def __getitem__(self, key):
        # wrap on the way out too, so nesting works in either style all the way down
        return _wrap(dict.__getitem__(self, key))

    def __setattr__(self, name, value):
        dict.__setitem__(self, name, value)


def _wrap(value):
    if isinstance(value, dict):
        return Obs(value)
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


def build_globals(obs: dict) -> dict:
    import builtins
    import datetime as _datetime
    import math, statistics, json as _json
    import numpy as np, pandas as pd
    try:
        import scipy as _scipy
        import scipy.stats as _sps
    except Exception:
        _scipy = None
        _sps = None

    import time as _time

    modules = {"datetime": _datetime, "json": _json, "math": math, "time": _time,
               "numpy": np, "pandas": pd, "statistics": statistics}
    if _scipy is not None:
        modules["scipy"] = _scipy
    if _sps is not None:
        modules["scipy.stats"] = _sps
    safe_builtins = dict(vars(builtins))
    safe_builtins["__import__"] = _restricted_imports(modules)
    # Restricting imports while leaving `open` in place is not a boundary: the
    # program can still read any file the process can, and credentials live in one.
    for name in ("open", "input", "breakpoint", "exit", "quit", "help"):
        safe_builtins.pop(name, None)

    g = {"__builtins__": safe_builtins, "math": math, "statistics": statistics,
         "time": _time, "json": _json, "np": np, "numpy": np, "pd": pd, "pandas": pd,
         "scipy": _scipy, "scipy_stats": _sps, "datetime": _datetime,
         "obs": _wrap(obs), "RpcError": RpcError}
    for ns in NAMESPACES:
        g[ns] = _Namespace(ns)
    return g


def main() -> int:
    payload = json.loads(sys.stdin.read())
    g = build_globals(payload.get("obs", {}))
    state_path = payload.get("state_path")
    if state_path and os.path.exists(state_path):     # namespace persists across rounds
        try:
            import pickle
            with open(state_path, "rb") as fh:
                g.update({k: v for k, v in pickle.load(fh).items()
                          if k not in g and not k.startswith("_")})
        except Exception:
            pass
    try:
        exec(compile(payload["code"], PROGRAM_FILENAME, "exec"), g)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if state_path:
            try:
                import pickle
                keep = {k: v for k, v in g.items()
                        if not k.startswith("_") and k not in ("obs", "RpcError")
                        and isinstance(v, (int, float, str, bool, list, dict, tuple, type(None)))}
                with open(state_path, "wb") as fh:
                    pickle.dump(keep, fh)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
