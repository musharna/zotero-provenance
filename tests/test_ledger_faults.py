"""A ledger that cannot be read is a fault, not a clean bill of health.

Every reader caught `sqlite3.DatabaseError` and returned an empty answer, so a
truncated, half-written or overwritten ledger reported "no open integrity
incidents" and exited 0. The evidence store for integrity incidents treated
corruption of itself as proof of integrity.

A MISSING ledger is different and stays silent: a healthy install never opens an
incident, so it never creates the file. Absent means zero. Unreadable means
unknown, and unknown is not zero.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess

from conftest import clean_env
import sys
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT
from zotero_capture.health_ledger import (
    acknowledge,
    acknowledge_all,
    count_open,
    open_incident,
    open_incidents,
)

CHECKER = PLUGIN_ROOT / "scripts" / "zotero_capture_health.py"


def _healthy(db: Path) -> Path:
    open_incident(
        db,
        incident_id="a1",
        url="https://fixturehost.org/one",
        root="/c/OLD",
        pinned_root="/c/NEW",
        kind="stale",
        ts="2026-08-26T00:00:00-04:00",
    )
    return db


def _corrupt(db: Path) -> Path:
    """A real SQLite file with its pages overwritten — what a torn write leaves."""
    _healthy(db)
    with db.open("r+b") as fh:
        fh.seek(0)
        fh.write(b"\x00" * 512)
    return db


def test_a_corrupt_ledger_is_not_reported_as_zero_incidents(tmp_path: Path) -> None:
    assert count_open(_healthy(tmp_path / "ok.db")) == 1, (
        "positive control: a readable ledger must still be counted"
    )
    with pytest.raises(sqlite3.DatabaseError):
        count_open(_corrupt(tmp_path / "bad.db"))


def test_a_corrupt_ledger_is_not_listed_as_empty(tmp_path: Path) -> None:
    assert len(open_incidents(_healthy(tmp_path / "ok.db"))) == 1, (
        "positive control: a readable ledger must still list its incidents"
    )
    with pytest.raises(sqlite3.DatabaseError):
        open_incidents(_corrupt(tmp_path / "bad.db"))


def test_acknowledging_into_a_corrupt_ledger_does_not_report_success(
    tmp_path: Path,
) -> None:
    assert acknowledge(_healthy(tmp_path / "ok.db"), ["a1"]) == ["a1"], (
        "positive control: a readable ledger must still resolve its incidents"
    )
    with pytest.raises(sqlite3.DatabaseError):
        acknowledge(_corrupt(tmp_path / "bad.db"), ["a1"])
    with pytest.raises(sqlite3.DatabaseError):
        acknowledge_all(_corrupt(tmp_path / "bad2.db"))


def test_a_missing_ledger_is_still_silence(tmp_path: Path) -> None:
    """Absent is a real answer: a healthy install never creates the file."""
    absent = tmp_path / "absent.db"
    assert open_incidents(absent) == []
    assert count_open(absent) == 0
    assert acknowledge(absent, ["a1"]) == []
    assert acknowledge_all(absent) == 0


def _run(state: Path, *args: str):
    env = clean_env(state)
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _state(tmp_path: Path, *, corrupt: bool) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    # A log holding nothing the importer can act on, so this exercises the
    # readers and not the migration. This used to write a `health-migrated`
    # marker to force that; the marker is gone -- it was a second copy of a
    # truth the ledger already holds, and it outlived the ledger it described.
    (state / "capture.log").write_text(
        json.dumps({"ts": "2026-08-26T00:00:00-04:00", "event": "noop"}) + "\n"
    )
    db = state / "health.db"
    return _corrupt(db) if corrupt else _healthy(db)


def test_the_checker_reports_a_corrupt_ledger_instead_of_a_clean_bill(
    tmp_path: Path,
) -> None:
    ok = _run(_state(tmp_path / "ok", corrupt=False).parent, "--list-incidents")
    assert ok.returncode == 0 and "a1" in ok.stdout, (
        f"positive control: a readable ledger must list its incident: {ok!r}"
    )

    bad = _run(_state(tmp_path / "bad", corrupt=True).parent, "--list-incidents")
    assert bad.returncode != 0, f"a corrupt ledger reported success: {bad.stdout!r}"
    assert "no open integrity incidents" not in bad.stdout, (
        "a corrupt ledger claimed there was nothing to report"
    )


def test_the_session_report_does_not_call_a_corrupt_ledger_healthy(
    tmp_path: Path,
) -> None:
    """The SessionStart path: silence here is a clean bill of health."""
    ok = _run(_state(tmp_path / "ok", corrupt=False).parent)
    assert ok.returncode == 0 and "integrity incident" in ok.stdout, (
        f"positive control: an open incident must be reported: {ok!r}"
    )

    bad = _run(_state(tmp_path / "bad", corrupt=True).parent)
    assert not (bad.returncode == 0 and bad.stdout == ""), (
        "a corrupt ledger produced the same silence as a healthy one"
    )
