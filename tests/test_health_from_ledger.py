"""Integrity comes from the ledger; operational faults come from the log.

Splitting them is what makes the check bounded. Open incidents are a small
queryable set that shrinks when a person resolves one. Operational faults stay
in the log because they decay on their own and never need suppressing.

It also fixes three suppressions that the log-replay design could not:

- a future-dated record used to increment a clock counter and `continue`,
  discarding integrity evidence that does not depend on recency at all
- one old valid record kept the monitor silent while the log's tail was
  permanently unparseable
- an upgrade silently suppressed proven 0.15-0.18 incidents, because they carry
  pin evidence but no incident id
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zotero_capture.health import evaluate
from zotero_capture.health_ledger import open_incident

TZ = timezone(timedelta(hours=-4))
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=TZ)
WINDOW = timedelta(hours=24)


def _check(lines, ledger=None):
    return evaluate(lines, pinned_root="/c/NEW", now=NOW, window=WINDOW,
                    ledger_path=ledger)


def test_an_open_incident_is_reported_from_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "health.db"
    open_incident(ledger, incident_id="a1", url="https://x.test/1", root="/c/OLD",
                  pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    warnings = _check([], ledger)

    assert warnings, "an open incident was not reported"
    assert "a1" in warnings[0], warnings


def test_a_future_dated_record_does_not_suppress_its_own_incident(tmp_path: Path) -> None:
    """Integrity does not depend on recency, so a bad clock must not hide it."""
    ledger = tmp_path / "health.db"
    open_incident(ledger, incident_id="f1", url="u", root="/c/OLD",
                  pinned_root="/c/NEW", kind="stale", ts="2099-01-01T00:00:00-04:00")
    future = json.dumps({"ts": "2099-01-01T00:00:00-0400", "event": "forward-unresolved"})

    warnings = _check([future], ledger)

    assert any("f1" in w for w in warnings), warnings
    assert any("future" in w for w in warnings), warnings


def test_a_broken_tail_is_reported_even_with_older_valid_records(tmp_path: Path) -> None:
    """One historical record used to bless everything appended after it."""
    good = json.dumps(
        {
            "ts": "2026-08-20T09:00:00-0400", "version": "x", "root": "/c/NEW",
            "pinned_root": "/c/NEW", "project": "p", "urls_seen": 1,
            "urls_new": 1, "urls_recurring": 0, "errors": [], "incident_id": "ok",
        }
    )
    lines = [good, "Traceback (most recent call last):", "fatal", "fatal"]

    warnings = _check(lines)

    assert warnings, "a permanently broken telemetry tail was invisible"
    assert any("unreadable" in w or "readable" in w for w in warnings), warnings


def test_a_healthy_log_with_no_ledger_is_still_silent(tmp_path: Path) -> None:
    """Positive control: the new signals must not fire on a working install."""
    good = json.dumps(
        {
            "ts": "2026-08-25T11:00:00-0400", "version": "x", "root": "/c/NEW",
            "pinned_root": "/c/NEW", "project": "p", "urls_seen": 1,
            "urls_new": 1, "urls_recurring": 0, "errors": [], "incident_id": "ok",
        }
    )

    assert _check([good], tmp_path / "absent.db") == []
