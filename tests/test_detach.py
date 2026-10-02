"""#43: `--detach` hands the capture to a forked worker; a failed fork must not lose it.

The detached path itself is driven end to end in test_end_to_end.py (real hook,
real fork, slow fake Zotero). This file covers what that cannot reach without
breaking the host: the fork failing.
"""

from __future__ import annotations

import json
import os
import signal
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


def test_a_failed_second_fork_falls_back_in_the_hook_not_the_child(configured, monkeypatch) -> None:
    """PR #44 review: the second fork fails inside the intermediate child.

    That child is not the hook. Falling back to an inline capture there ran it with
    no deadline and no `timeout` above it, while the hook sat in waitpid on it. The
    first fork here is real, so the child really is a separate process.
    """
    real_fork = os.fork
    hook_pid = os.getpid()
    forks: list[int] = []

    def _second_fails() -> int:
        forks.append(os.getpid())
        if len(forks) == 1:
            return real_fork()
        raise OSError(11, "Resource temporarily unavailable")

    reached: list[int] = []

    def _client(*a, **k):
        reached.append(os.getpid())
        raise cli.HookTerminated("terminated by signal 15")

    monkeypatch.setattr(cli.os, "fork", _second_fails)
    monkeypatch.setattr(cli, "build_client", _client)
    try:
        rc = main(
            ["--cwd", "/tmp", "--session", "s", "--detach", "--message", "see https://x.test/a"]
        )
    finally:
        if os.getpid() != hook_pid:
            # A child that returned into main() must not run on into pytest.
            os._exit(0)

    assert rc == 0
    # The capture ran in the hook process, under the hook's own budget.
    assert reached == [hook_pid], reached
    failed = [e for e in _events(configured) if e["event"] == "detach-failed"]
    assert len(failed) == 1 and "intermediate child exited 1" in json.dumps(failed[0]), failed


def test_the_worker_arms_its_deadline_before_any_other_setup(monkeypatch) -> None:
    """A worker whose /dev/null setup fails still runs bounded by its deadline."""

    def _no_devnull(*a, **k):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(cli.os, "fork", lambda: 0)  # in-process: we are the worker
    monkeypatch.setattr(cli.os, "setsid", lambda: None)
    monkeypatch.setattr(cli.os, "open", _no_devnull)
    previous = signal.getsignal(signal.SIGALRM)
    try:
        with pytest.raises(OSError, match="Too many open files"):
            cli._detach()
        assert signal.getsignal(signal.SIGALRM) is cli._on_deadline
        assert signal.alarm(0) > 0, "the worker deadline was not armed"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
