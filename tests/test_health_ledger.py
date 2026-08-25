"""Open incidents live in a ledger, written BEFORE the mutation they describe.

Two findings force this shape.

The first is that the journal used to begin after the damage: the incident id
was created inside `_emit_log`, which runs after every Zotero write. A hook
timeout — and the hooks impose ten and fifteen seconds — could leave a row
written to the library from a superseded root with no record that it happened.
Worse, "did it write" was inferred from `urls_new + urls_recurring`, which are
COMPLETION counters incremented well after the POST returns, so a commit
followed by an ambiguous failure reported zero writes.

The second is information-theoretic: replaying an immutable, unbounded log and
suppressing acknowledged incidents requires remembering an unbounded set of ids.
There is no bounded lossless version of that. So the bounded thing is the set of
OPEN incidents, and acknowledgement resolves a row rather than accumulating a
key forever.

An intent recorded before a mutation that never happens is a false positive that
a person can close. A mutation that happens with no record is corruption nobody
can find. The asymmetry is the whole argument.
"""

from __future__ import annotations

from pathlib import Path

from zotero_capture.health_ledger import (
    acknowledge,
    acknowledge_all,
    count_open,
    open_incident,
    open_incidents,
)


def _db(tmp_path: Path) -> Path:
    return tmp_path / "health.db"


def test_an_incident_exists_before_the_write_it_describes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    open_incident(db, incident_id="a1", url="https://x.test/1", root="/c/OLD",
                  pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    found = open_incidents(db)

    assert [i["incident_id"] for i in found] == ["a1"]
    assert found[0]["url"] == "https://x.test/1"
    assert found[0]["kind"] == "stale"


def test_recording_the_same_incident_twice_is_idempotent(tmp_path: Path) -> None:
    """The same write may be retried; it is still one incident."""
    db = _db(tmp_path)
    for _ in range(3):
        open_incident(db, incident_id="a1", url="u", root="/c/OLD",
                      pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    assert count_open(db) == 1


def test_acknowledging_resolves_exactly_one_incident(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for i in ("a1", "a2"):
        open_incident(db, incident_id=i, url="u", root="/c/OLD",
                      pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    assert acknowledge(db, ["a1"]) == ["a1"]
    assert [i["incident_id"] for i in open_incidents(db)] == ["a2"]


def test_acknowledging_an_unknown_id_changes_nothing(tmp_path: Path) -> None:
    """It used to exit 0, print "acknowledged 1 incident(s)", and store the typo."""
    db = _db(tmp_path)
    open_incident(db, incident_id="a1", url="u", root="/c/OLD",
                  pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    assert acknowledge(db, ["nope"]) == []
    assert count_open(db) == 1, "an unknown id disturbed the ledger"


def test_acknowledging_a_mixed_batch_reports_only_what_it_resolved(tmp_path: Path) -> None:
    db = _db(tmp_path)
    open_incident(db, incident_id="a1", url="u", root="/c/OLD",
                  pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    assert acknowledge(db, ["a1", "nope"]) == ["a1"]
    assert count_open(db) == 0


def test_ack_all_resolves_every_open_incident(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for i in ("a1", "a2", "a3"):
        open_incident(db, incident_id=i, url="u", root="/c/OLD",
                      pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    assert acknowledge_all(db) == 3
    assert count_open(db) == 0


def test_an_acknowledged_incident_does_not_reopen(tmp_path: Path) -> None:
    """The write is still in the log; re-ingesting it must not undo the ack."""
    db = _db(tmp_path)
    open_incident(db, incident_id="a1", url="u", root="/c/OLD",
                  pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")
    acknowledge(db, ["a1"])

    open_incident(db, incident_id="a1", url="u", root="/c/OLD",
                  pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    assert count_open(db) == 0, "an acknowledged incident came back"


def test_listing_is_bounded_and_reports_what_it_did_not_show(tmp_path: Path) -> None:
    """A silent cap recreates hidden incidents; an explicit remainder does not."""
    db = _db(tmp_path)
    for n in range(10):
        open_incident(db, incident_id=f"a{n}", url="u", root="/c/OLD",
                      pinned_root="/c/NEW", kind="stale", ts="2026-08-25T11:00:00-04:00")

    shown = open_incidents(db, limit=3)

    assert len(shown) == 3
    assert count_open(db) == 10


def test_a_missing_ledger_is_empty_not_an_error(tmp_path: Path) -> None:
    assert open_incidents(tmp_path / "absent.db") == []
    assert count_open(tmp_path / "absent.db") == 0
