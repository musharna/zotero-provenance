"""Append-on-fail JSONL queue for Zotero writes that need retry."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def append_failure(queue_path: Path, entry: dict[str, Any]) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def drain_queue(
    queue_path: Path,
    handler: Callable[[dict[str, Any]], bool],
) -> int:
    """Run handler on each queued entry. Keep entries where handler returns False or raises."""
    if not queue_path.exists():
        return 0
    succeeded = 0
    keep: list[str] = []
    for line in queue_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            ok = handler(entry)
        except Exception:
            ok = False
        if ok:
            succeeded += 1
        else:
            keep.append(line)
    if keep:
        queue_path.write_text("\n".join(keep) + "\n")
    else:
        queue_path.unlink(missing_ok=True)
    return succeeded
