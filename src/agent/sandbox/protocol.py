"""Line-delimited JSON RPC between host and sandbox."""
from __future__ import annotations

import json
from typing import Any


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, default=str) + "\n").encode()


def decode(line: bytes) -> dict:
    return json.loads(line.decode())


class RpcError(RuntimeError):
    """Raised inside the child when the host refuses or fails a capability call."""


def call_frame(ns: str, fn: str, args: list, kwargs: dict) -> dict:
    return {"ns": ns, "fn": fn, "args": args, "kwargs": kwargs}


def ok_frame(value: Any) -> dict:
    return {"ok": True, "value": value}


def err_frame(kind: str, message: str) -> dict:
    return {"ok": False, "error": kind, "message": message}
