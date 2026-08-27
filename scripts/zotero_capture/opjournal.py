"""A record of what a maintenance run was doing, and where it stopped.

`retire` has journalled the rows it destroys since it was written, because
Zotero's trash restores an item but not its sighting history. Nothing else did.
That left two gaps, and they are not the same size:

  * `prune` trashes items with no record of which ones. Recoverable in practice —
    the items are in Zotero's trash — but "which of these did that run put here"
    is unanswerable, which is the question you have when a pass surprises you.

  * `repair` REWRITES a URL, on the Zotero item and on the index row, and the
    previous address is recorded nowhere. Not in a trash, not in a journal; it
    exists only in the in-memory plan and on stdout. A trashed item can be
    restored by a human who disagrees. An overwritten URL cannot, because
    nothing on disk remembers what it was.

  * And no run of any kind marked its own start or finish, so an interrupted
    pass — SIGTERM, a dropped connection, a laptop lid — left no way to ask
    where it got to. The counts printed at the end are the only record, and an
    interrupted run never prints them.

So this journals the BEFORE state of each step ahead of the mutation, and frames
each run with a start and an end. A run that started and never ended is exactly
an interrupted one; that is what `unfinished_operations` reads.

Append-only JSONL, one line per event, flushed per line. A journal that buffers
is a journal that loses precisely the tail you needed — the steps closest to the
interruption.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path


def journal_path(db_path: Path) -> Path:
    """Beside the index, like the retire journal it generalises."""
    return db_path.with_name(f"{db_path.name}.operations.jsonl")


class OperationJournal:
    """One maintenance run. Use as a context manager so the end is not optional.

    The end record is written even when the body raises, with the exception
    named: a run that died is finished, and marking it unfinished forever would
    turn a real signal into permanent noise. Only a process that never got to
    run its handlers at all — SIGKILL, a power cut — leaves an open run, which
    is the case worth reporting.
    """

    def __init__(self, db_path: Path, command: str, *, args: str = "") -> None:
        self.path = journal_path(db_path)
        self.op_id = uuid.uuid4().hex
        self.command = command
        self.args = args
        self.seq = 0

    def _write(self, record: dict) -> None:
        record["op_id"] = self.op_id
        record["command"] = self.command
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def __enter__(self) -> "OperationJournal":
        self._write({"event": "start", "args": self.args, "pid": os.getpid()})
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._write(
            {
                "event": "end",
                "steps": self.seq,
                "failed": None if exc_type is None else f"{exc_type.__name__}: {exc}",
            }
        )
        return False

    def step(self, *, target: str, action: str, before: dict | None = None) -> int:
        """Record a mutation BEFORE it happens, with what it is about to replace.

        `before` is the part that makes this a journal rather than a log. For a
        repair it carries the URL being overwritten, which exists nowhere else
        once the write lands.
        """
        self.seq += 1
        self._write(
            {
                "event": "step",
                "seq": self.seq,
                "target": target,
                "action": action,
                "before": before or {},
            }
        )
        return self.seq

    def outcome(self, seq: int, state: str, detail: str = "") -> None:
        """Close one step: done, refused, or failed."""
        self._write(
            {"event": "outcome", "seq": seq, "state": state, "detail": detail[:500]}
        )


def read_events(db_path: Path) -> list[dict]:
    path = journal_path(db_path)
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            # A torn final line is what an interruption looks like. Skipping it
            # is right; treating the whole journal as unreadable is not.
            continue
    return events


def unfinished_operations(db_path: Path) -> list[dict]:
    """Runs that started and never ended, newest first.

    A run whose body raised still ends — with the exception recorded — so this
    reports only the ones that could not run their handlers at all.
    """
    by_op: dict[str, dict] = {}
    ended: set[str] = set()
    steps: dict[str, list[dict]] = defaultdict(list)
    for event in read_events(db_path):
        op_id = event.get("op_id")
        if not op_id:
            continue
        if event.get("event") == "start":
            by_op[op_id] = event
        elif event.get("event") == "end":
            ended.add(op_id)
        elif event.get("event") == "step":
            steps[op_id].append(event)

    out = []
    for op_id, start in by_op.items():
        if op_id in ended:
            continue
        op_steps = steps.get(op_id, [])
        out.append(
            {
                "op_id": op_id,
                "command": start.get("command"),
                "started": start.get("ts"),
                "args": start.get("args", ""),
                "steps": len(op_steps),
                "last_target": op_steps[-1]["target"] if op_steps else None,
            }
        )
    return sorted(out, key=lambda o: o["started"] or "", reverse=True)
