"""Small, atomic checkpoint for state that changes future decisions.

Execution and theses have their own append-only ledgers.  This file is only the
replaceable operational snapshot: trigger scheduling, compact bundle history, and
the immutable campaign equity baseline.  Staged drafts never enter this schema.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


SCHEMA_VERSION = 1


def write(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    data = json.dumps({"schema_version": SCHEMA_VERSION, **payload},
                      sort_keys=True, default=str)
    with temporary.open("w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, target)
    # Make the rename durable as well as the file contents.
    directory_fd = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read(path: str | Path) -> dict | None:
    target = Path(path)
    if not target.exists():
        return None
    raw = json.loads(target.read_text())
    if int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError(f"unsupported runtime state schema {raw.get('schema_version')!r}")
    return raw
