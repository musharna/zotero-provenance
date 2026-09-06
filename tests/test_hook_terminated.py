"""A hook timeout is a fact about US, and it must leave a structured record.

`HookTerminated` derives from BaseException so the per-URL loop cannot swallow
it, and `main()` caught only `Exception`, so the timeout escaped as a raw
traceback into capture.log. Health then read those lines as "unreadable log"
and the next successful capture erased the finding. The live capture.log held
two of them when this was written (2026-09-06).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import zotero_capture.cli as cli
from zotero_capture.cli import main
from zotero_capture.health import REFUSAL_EVENTS


def _events(tmp_path: Path) -> list[dict]:
    try:
        text = (tmp_path / "capture.log").read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_WEBSOURCES_COLLECTION_KEY", "C")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.delenv("ZOTERO_CAPTURE_DISABLE", raising=False)

    def _cut_off(*a, **k):
        raise cli.HookTerminated("terminated by signal 15")

    monkeypatch.setattr(cli, "build_client", _cut_off)
    return tmp_path


def test_a_timeout_writes_a_hook_terminated_event(configured) -> None:
    rc = main(["--cwd", "/tmp", "--session", "s", "--message", "see https://x.test/a"])

    assert rc == 0, "a hook timeout must not block a turn"
    events = [e for e in _events(configured) if e.get("event") == "hook-terminated"]
    assert events, _events(configured)
    assert "signal 15" in str(events[0].get("detail")), events[0]
    # Positive control: an event health does not count is written and never
    # reported, which is the defect one layer up.
    assert "hook-terminated" in REFUSAL_EVENTS


def test_the_traceback_no_longer_reaches_the_log(configured, capsys) -> None:
    main(["--cwd", "/tmp", "--message", "see https://x.test/a"])
    assert "Traceback" not in capsys.readouterr().err
