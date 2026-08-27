"""The legacy import is replay-safe, so it must not be gated by a marker file.

`open_incident` states the contract in its own docstring: "Idempotent, and it
will not resurrect an acknowledged incident: the same write may be retried, and
the log it came from is replayed on every start." The ledger enforces that with
`incident_id` as PRIMARY KEY, `ON CONFLICT DO NOTHING`, and an acknowledgement
that UPDATEs status rather than deleting the row.

A `health-migrated` marker file was nonetheless gating the import to once, ever.
That is a second copy of a truth the ledger already holds, and the two can
disagree: delete `health.db` and the marker survives, so the ledger never
rebuilds and any legacy incident in the log stays invisible forever. On this
machine the marker read `0` with `health.db` absent — harmless only because the
log genuinely holds no legacy incidents, which was measured rather than assumed.

The fix removes the marker instead of guarding it. Round 8 punished the same
shape twice: "two removed a stale second copy of the truth instead of guarding
it." State that cannot disagree is better than state kept in agreement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import PLUGIN_ROOT

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from zotero_capture.health_ledger import (  # noqa: E402
    acknowledge,
    count_open,
    open_incidents,
)
from zotero_capture_health import _migrate_legacy  # noqa: E402

PINNED = "/cache/zotero-provenance/zotero-provenance/0.24.0"
SUPERSEDED = "/cache/zotero-provenance/zotero-provenance/0.16.0"


def _state_with_legacy_record(tmp_path: Path) -> Path:
    """A pre-0.19 record: wrote from a root that was not pinned, and carries no
    incident_id of its own, so only the importer can give it one."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "capture.log").write_text(
        json.dumps(
            {
                "ts": "2026-08-20T10:00:00-0400",
                "version": "0.16.0",
                "root": SUPERSEDED,
                "pinned_root": PINNED,
                "urls_seen": 2,
                "urls_new": 2,
                "errors": [],
            }
        )
        + "\n"
    )
    return state


def test_a_legacy_record_is_imported_at_all(tmp_path: Path) -> None:
    """Positive control. Without it every assertion below passes vacuously on a
    detector that finds nothing."""
    state = _state_with_legacy_record(tmp_path)
    ledger = state / "health.db"

    _migrate_legacy(state, ledger, PINNED)

    assert count_open(ledger) == 1
    assert open_incidents(ledger)[0]["incident_id"].startswith("legacy:")


def test_a_deleted_ledger_rebuilds_from_the_log(tmp_path: Path) -> None:
    """The defect. A marker that outlives the ledger it describes means the
    incident is gone for good."""
    state = _state_with_legacy_record(tmp_path)
    ledger = state / "health.db"
    _migrate_legacy(state, ledger, PINNED)
    assert count_open(ledger) == 1

    ledger.unlink()
    _migrate_legacy(state, ledger, PINNED)

    assert count_open(ledger) == 1, "the deleted ledger never rebuilt"


def test_re_running_does_not_reopen_an_acknowledged_incident(tmp_path: Path) -> None:
    """The reason a marker looked necessary, and the reason it is not.

    Resurrecting acknowledged incidents is the permanent-chatter failure 0.15.0
    introduced and 0.16.0 had to cut back. The PRIMARY KEY plus ON CONFLICT DO
    NOTHING is what actually prevents it — not the marker.
    """
    state = _state_with_legacy_record(tmp_path)
    ledger = state / "health.db"
    _migrate_legacy(state, ledger, PINNED)
    incident_id = open_incidents(ledger)[0]["incident_id"]
    assert acknowledge(ledger, [incident_id]) == [incident_id]
    assert count_open(ledger) == 0

    _migrate_legacy(state, ledger, PINNED)

    assert count_open(ledger) == 0, "a replay reopened an acknowledged incident"


def test_the_import_is_idempotent(tmp_path: Path) -> None:
    state = _state_with_legacy_record(tmp_path)
    ledger = state / "health.db"

    for _ in range(3):
        _migrate_legacy(state, ledger, PINNED)

    assert count_open(ledger) == 1, "replaying the log duplicated the incident"


def test_no_marker_file_is_written(tmp_path: Path) -> None:
    """The marker is gone, not merely ignored. A file still being written is a
    second copy of the truth waiting for a future reader to trust it."""
    state = _state_with_legacy_record(tmp_path)

    _migrate_legacy(state, state / "health.db", PINNED)

    assert not (state / "health-migrated").exists()
