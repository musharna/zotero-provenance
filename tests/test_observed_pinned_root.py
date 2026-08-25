"""Each capture records what the registry said WHEN IT WROTE.

`installed_at` is not a generation clock. It answers "was this capture before or
after the current install", which forgives every earlier stale write the moment
anything upgrades or reinstalls:

    T0  0.13.0 pinned
    T1  0.3.0 captures — already stale, already dangerous
    T2  upgrade to 0.15.0; lastUpdated becomes T2
    T3  health runs, sees T1 < T2, says nothing

The durable answer is for the log line to carry the pinned root observed at the
moment of the write. Then staleness is a property of the record itself, decided
once, by the process that was actually there — not re-derived later against a
registry that has since moved.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zotero_capture.capture import CaptureResult
from zotero_capture.cli import _emit_log
from zotero_capture.health import evaluate

TZ = timezone(timedelta(hours=-4))


def _line(ts: str, root: str, pinned: str | None) -> str:
    obj = {
        "ts": ts,
        "version": "x",
        "root": root,
        "project": "demo",
        "urls_seen": 1,
        "urls_new": 1,
        "errors": [],
    }
    if pinned is not None:
        obj["pinned_root"] = pinned
    return json.dumps(obj)


def test_the_log_line_carries_the_pinned_root(tmp_path: Path, monkeypatch) -> None:
    import zotero_capture.cli as cli

    monkeypatch.setattr(cli, "_observed_pinned_root", lambda: "/c/0.15.0")
    log = tmp_path / "capture.log"
    _emit_log(log, project="demo", context=None, result=CaptureResult(), latency_ms=1)

    record = json.loads(log.read_text().splitlines()[-1])
    assert record.get("pinned_root") == "/c/0.15.0", record


def test_an_upgrade_does_not_forgive_a_write_that_was_stale_at_the_time() -> None:
    """The T1 write above stays visible after the T2 upgrade."""
    stale_then = _line("2026-08-24T10:00:00-04:00", "/c/0.3.0", "/c/0.13.0")
    warnings = evaluate(
        [stale_then],
        pinned_root="/c/0.15.0",
        now=datetime(2026, 8, 25, 12, 0, tzinfo=TZ),
        installed_at=datetime(2026, 8, 25, 11, 30, tzinfo=TZ),
    )

    assert warnings, "an upgrade erased evidence of a write that was already stale"
    assert "0.3.0" in warnings[0], warnings


def test_a_write_that_was_current_at_the_time_stays_quiet() -> None:
    """The legitimate case: it agreed with the registry when it ran."""
    fine = _line("2026-08-24T10:00:00-04:00", "/c/0.13.0", "/c/0.13.0")
    warnings = evaluate(
        [fine],
        pinned_root="/c/0.15.0",
        now=datetime(2026, 8, 25, 12, 0, tzinfo=TZ),
        installed_at=datetime(2026, 8, 25, 11, 30, tzinfo=TZ),
    )

    assert warnings == [], warnings


def test_records_without_the_field_fall_back_to_the_install_clock() -> None:
    """Every line written before 0.15.0 lacks it; they must still be readable."""
    old = _line("2026-08-24T10:00:00-04:00", "/c/0.3.0", None)
    warnings = evaluate(
        [old],
        pinned_root="/c/0.15.0",
        now=datetime(2026, 8, 25, 12, 0, tzinfo=TZ),
        installed_at=datetime(2026, 8, 25, 11, 30, tzinfo=TZ),
    )

    assert warnings == [], warnings
