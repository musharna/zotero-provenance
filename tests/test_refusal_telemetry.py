"""A refusal must not be indistinguishable from a successful capture.

Two refusal paths — a superseded plugin version, and an index bound to a
different library — logged an error and returned an empty CaptureResult. The CLI
then emitted an ordinary capture line: `urls_seen: 0, errors: []`. That is the
exact shape of a healthy message containing no citable URL, so the health check
counted a refusal as a success, advanced its clock and reported nothing.

An index-identity mismatch can refuse every citation forever while writing a
fresh, healthy-looking record each time. Found by the 2026-08-25 Codex audit;
the plugin's own instrumentation could not see its own most deliberate failures.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import zotero_capture.capture as cap
from zotero_capture.capture import CaptureResult, capture_message
from zotero_capture.cli import _emit_log
from zotero_capture.health import evaluate


class _NoZotero:
    def __getattr__(self, name):  # pragma: no cover - must never be reached
        raise AssertionError(f"a refused capture must not call Zotero ({name})")


def _call(db: Path) -> CaptureResult:
    return capture_message(
        message="see https://example.com/real",
        project_slug="demo",
        context=None,
        today=date(2026, 8, 25),
        db_path=db,
        zotero=_NoZotero(),
        title_fetcher=lambda url: "t",
    )


def test_a_superseded_version_records_why_it_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cap, "installed_version", lambda: "9.9.9")

    result = _call(tmp_path / "i.db")

    assert result.refused, "a refusal left no trace on the result"
    assert "9.9.9" in result.refused


def test_the_log_line_says_it_was_refused(tmp_path: Path) -> None:
    log = tmp_path / "capture.log"
    _emit_log(
        log,
        project="demo",
        context=None,
        result=CaptureResult(refused="running 0.3.0, 0.15.0 is installed"),
        latency_ms=1,
    )

    record = json.loads(log.read_text().splitlines()[-1])
    assert record.get("refused"), record


def test_health_does_not_count_a_refusal_as_a_capture(tmp_path: Path) -> None:
    """The whole point: a refusal must break the silence, not reset it."""
    tz = timezone(timedelta(hours=-4))
    refused = json.dumps(
        {
            "ts": "2026-08-25T11:59:00-04:00",
            "version": "0.3.0",
            "root": "/c/0.3.0",
            "project": "demo",
            "urls_seen": 0,
            "urls_new": 0,
            "errors": [],
            "refused": "running 0.3.0, 0.15.0 is installed",
        }
    )
    good = json.dumps(
        {
            "ts": "2026-08-23T09:00:00-04:00",
            "version": "0.15.0",
            "root": "/c/0.15.0",
            "project": "demo",
            "urls_seen": 1,
            "urls_new": 1,
            "errors": [],
        }
    )

    warnings = evaluate(
        [good, refused],
        pinned_root="/c/0.15.0",
    )

    assert warnings, "a refusal was read as a successful capture"
    assert any("refus" in w for w in warnings), warnings
