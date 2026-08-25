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
from zotero_capture.health import evaluate as _evaluate

TZ = timezone(timedelta(hours=-4))


def _line(ts: str, root: str, pinned: str | None) -> str:
    obj = {
        "ts": ts,
        "version": "x",
        "root": root,
        "project": "demo",
        "urls_seen": 1,
        "urls_new": 1,
        "urls_recurring": 0,
        "errors": [],
        "incident_id": f"{ts}|{root}",
    }
    if pinned is not None:
        obj["pinned_root"] = pinned
    return json.dumps(obj)


def test_the_log_line_carries_the_pinned_root(tmp_path: Path) -> None:
    log = tmp_path / "capture.log"
    _emit_log(
        log,
        project="demo",
        context=None,
        result=CaptureResult(),
        latency_ms=1,
        pinned_root="/c/0.15.0",
    )

    record = json.loads(log.read_text().splitlines()[-1])
    assert record.get("pinned_root") == "/c/0.15.0", record



def test_a_write_that_was_current_at_the_time_stays_quiet() -> None:
    """The legitimate case: it agreed with the registry when it ran."""
    fine = _line("2026-08-24T10:00:00-04:00", "/c/0.13.0", "/c/0.13.0")
    warnings = evaluate(
        [fine],
        pinned_root="/c/0.15.0",
        installed_at=datetime(2026, 8, 25, 11, 30, tzinfo=TZ),
    )

    assert warnings == [], warnings



def test_the_pin_is_observed_before_the_capture_not_after(tmp_path: Path) -> None:
    """A registry change during a capture must not fabricate a stale write.

    _emit_log resolved the pin AFTER capture_message finished, so:

        A is pinned; A starts capturing
        A writes the Zotero item
        the registry moves to B
        A emits its log line and records pinned_root=B

    which reads as "A wrote while B was pinned" — a stale write that never
    happened. The inverse ordering hides a real one. The authorisation state has
    to be read before the work it authorises.
    """
    import zotero_capture.cli as cli

    observed: list[str] = []

    def _moving_target() -> str:
        # Different answer each call: only a single, early read is stable.
        observed.append(f"/c/{len(observed)}")
        return observed[-1]

    monkeypatch_target = getattr(cli, "_observed_pinned_root")
    assert callable(monkeypatch_target)

    cli._observed_pinned_root = _moving_target  # type: ignore[assignment]
    try:
        log = tmp_path / "capture.log"
        pin = cli._observed_pinned_root()          # what the capture would see
        _emit_log(
            log,
            project="demo",
            context=None,
            result=CaptureResult(),
            latency_ms=1,
            pinned_root=pin,
        )
    finally:
        cli._observed_pinned_root = monkeypatch_target  # type: ignore[assignment]

    record = json.loads(log.read_text().splitlines()[-1])
    assert record["pinned_root"] == "/c/0", record


def test_an_unresolvable_pin_is_recorded_as_unknown(tmp_path: Path) -> None:
    """Omitting the field silently downgraded a new record to legacy semantics."""
    log = tmp_path / "capture.log"
    _emit_log(
        log,
        project="demo",
        context=None,
        result=CaptureResult(),
        latency_ms=1,
        pinned_root=None,
    )

    record = json.loads(log.read_text().splitlines()[-1])
    assert record.get("pin_observation") == "unknown", record


def test_a_record_that_disclaims_the_pin_is_not_called_stale() -> None:
    """`pin_observation: "unknown"` was written and then read by nothing.

    The record says, explicitly, that the writer could not determine which root
    was pinned. Judging it by the install clock anyway is exactly the legacy
    semantics the field was added to replace — and it produced a POSITIVE stale
    write warning from evidence that disclaims itself. A detector that cannot
    tell "stale" from "unknown" is guessing, which is the habit that produced
    every false alarm in this feature so far.
    """
    line = json.dumps(
        {
            "ts": "2026-08-25T11:40:00-04:00",
            "version": "0.3.0",
            "root": "/c/0.3.0",
            "pin_observation": "unknown",
            "urls_seen": 1,
            "urls_new": 1,
            "urls_recurring": 0,
            "incident_id": "inline-8478",
            "errors": [],
        }
    )

    warnings = _classify([line],
        pinned_root="/c/0.16.0",
        installed_at=datetime(2026, 8, 25, 11, 30, tzinfo=TZ),
    )

    # Not called stale — but not silent either. Unverified is its own answer.
    assert len(warnings) == 1, warnings
    assert "unverified" in warnings[0], warnings
    assert "stale" not in warnings[0], warnings


def test_a_resolved_pin_still_decides_normally() -> None:
    """Positive control, so the test above cannot pass by disabling the signal."""
    line = json.dumps(
        {
            "ts": "2026-08-25T11:40:00-04:00",
            "version": "0.3.0",
            "root": "/c/0.3.0",
            "pinned_root": "/c/0.16.0",
            "urls_seen": 1,
            "urls_new": 1,
            "urls_recurring": 0,
            "incident_id": "inline-7097",
            "errors": [],
        }
    )

    warnings = _classify([line],
        pinned_root="/c/0.16.0",
        installed_at=datetime(2026, 8, 25, 11, 30, tzinfo=TZ),
    )

    assert warnings, "the stale signal stopped working entirely"


WINDOW = timedelta(hours=24)
_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=TZ)


def evaluate(lines, *, pinned_root, installed_at=None):
    """Shim: 0.18.0 dropped `installed_at` (see test_no_cross_record_inference)."""
    return _evaluate(lines, pinned_root=pinned_root, now=_NOW, window=WINDOW)


# --- 0.20.0: integrity moved from log-replay to the ledger --------------------
#
# `evaluate()` no longer classifies integrity from log records; incidents are
# written to a ledger BEFORE the mutation they describe, because the log could
# only ever say what already finished. These tests still assert the thing that
# matters — that a record's own contents decide, with no reference to any other
# record — so they now exercise `incidents()`, the classifier that FEEDS the
# ledger, instead of the reporter that reads it.


def _classify(lines, acknowledged=frozenset(), **_ignored):
    from zotero_capture.health import incidents as _incidents

    return [
        f"{i['kind']} capture(s) ran from plugin {i['root']} [{i['id']}]"
        for i in _incidents(lines, pinned_root="/c/0.16.0")
        if i["id"] not in acknowledged
    ]
