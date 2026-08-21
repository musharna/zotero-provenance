"""retry_queue tests — JSONL append-on-fail; drain-on-entry."""

from __future__ import annotations

import json
from pathlib import Path

from zotero_capture.retry_queue import append_failure, drain_queue


def test_append_failure_writes_jsonl(tmp_path: Path):
    queue = tmp_path / "retry.jsonl"
    append_failure(queue, {"url": "https://fixturehost.org/x", "error": "503"})
    append_failure(queue, {"url": "https://fixturehost.org/y", "error": "504"})
    lines = queue.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["url"] == "https://fixturehost.org/x"


def test_drain_calls_handler_and_removes(tmp_path: Path):
    queue = tmp_path / "retry.jsonl"
    append_failure(queue, {"url": "https://fixturehost.org/x", "error": "503"})
    append_failure(queue, {"url": "https://fixturehost.org/y", "error": "503"})

    drained: list[str] = []

    def handler(entry: dict) -> bool:
        drained.append(entry["url"])
        return True

    n = drain_queue(queue, handler)
    assert n == 2
    assert sorted(drained) == ["https://fixturehost.org/x", "https://fixturehost.org/y"]
    assert not queue.exists() or queue.read_text() == ""


def test_drain_keeps_malformed_lines_and_continues(tmp_path: Path):
    queue = tmp_path / "retry.jsonl"
    queue.write_text(
        'not valid json\n{"url": "https://fixturehost.org/x", "error": "503"}\n'
    )

    drained: list[str] = []

    def handler(entry: dict) -> bool:
        drained.append(entry["url"])
        return True

    n = drain_queue(queue, handler)
    assert n == 1
    assert drained == ["https://fixturehost.org/x"]
    remaining = queue.read_text().splitlines()
    assert remaining == ["not valid json"]


def test_drain_keeps_failed_entries(tmp_path: Path):
    queue = tmp_path / "retry.jsonl"
    append_failure(queue, {"url": "https://fixturehost.org/x", "error": "503"})
    append_failure(queue, {"url": "https://fixturehost.org/y", "error": "503"})

    def handler(entry: dict) -> bool:
        return entry["url"].endswith("/x")

    n = drain_queue(queue, handler)
    assert n == 1
    remaining = queue.read_text().splitlines()
    assert len(remaining) == 1
    assert json.loads(remaining[0])["url"] == "https://fixturehost.org/y"
