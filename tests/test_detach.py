"""#43: `--detach` hands the capture to a forked worker; a failed fork must not lose it.

The detached path itself is driven end to end in test_end_to_end.py (real hook,
real fork, slow fake Zotero). This file covers what that cannot reach without
breaking the host: the fork failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import zotero_capture.cli as cli
from zotero_capture.cli import main
from zotero_capture.health import REFUSAL_EVENTS


def _events(state: Path) -> list[dict]:
    try:
        text = (state / "capture.log").read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return [r for r in out if r.get("event")]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_WEBSOURCES_COLLECTION_KEY", "C")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.delenv("ZOTERO_CAPTURE_DISABLE", raising=False)
    return tmp_path


def test_a_failed_fork_is_recorded_and_the_capture_runs_inline(configured, monkeypatch) -> None:
    def _no_fork() -> int:
        raise OSError(11, "Resource temporarily unavailable")

    reached: list[bool] = []

    def _client(*a, **k):
        # Reaching the client is the proof the capture went on inline; ending in
        # the hook-timeout path keeps the test off the network.
        reached.append(True)
        raise cli.HookTerminated("terminated by signal 15")

    monkeypatch.setattr(cli.os, "fork", _no_fork)
    monkeypatch.setattr(cli, "build_client", _client)

    rc = main(["--cwd", "/tmp", "--session", "s", "--detach", "--message", "see https://x.test/a"])

    assert rc == 0, "a failed fork must not block a turn"
    kinds = [e["event"] for e in _events(configured)]
    assert "detach-failed" in kinds, kinds
    # Positive control in the same test: the capture was not dropped.
    assert reached == [True] and "hook-terminated" in kinds, kinds
    # And health reports it rather than reading it as noise.
    assert "detach-failed" in REFUSAL_EVENTS


def test_without_detach_main_never_forks(configured, monkeypatch) -> None:
    """In-process callers (tests, triage) keep the synchronous path."""

    def _boom() -> int:
        raise AssertionError("main() forked without --detach")

    monkeypatch.setattr(cli.os, "fork", _boom)
    monkeypatch.setattr(
        cli, "build_client", lambda *a, **k: (_ for _ in ()).throw(cli.HookTerminated("t"))
    )

    assert main(["--cwd", "/tmp", "--session", "s", "--message", "see https://x.test/a"]) == 0
    assert "hook-terminated" in [e["event"] for e in _events(configured)]
