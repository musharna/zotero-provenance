"""The round-7 findings left unfixed when 0.21.0 shipped only the two blockers.

All six were re-verified against live source before this file was written; none
had drifted. They are grouped here because they share one theme -- the
write-ahead ledger's edges, where a claim, a path or a timestamp is trusted a
little further than it was actually established.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

import zotero_capture.capture as cap
from zotero_capture.capture import capture_message
from zotero_capture.sqlite_cache import init_db, lookup_url

URL = "https://fixturehost.org/one"


class _JournalRefuses:
    """The ledger cannot be written, so the write must not happen."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    def post_webpage_item(self, *, url_canonical, **kw):  # pragma: no cover
        self.posted.append(url_canonical)
        raise AssertionError("the POST must not be reached")

    def add_tags(self, *a, **k):  # pragma: no cover
        raise AssertionError("unexpected")

    def item_exists(self, key):  # pragma: no cover
        raise AssertionError("unexpected")


def test_a_url_is_not_stranded_when_its_journal_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """`issued = True` was set BEFORE _record_intent.

    So when journalling refused -- a read-only state dir, a full disk -- the
    handler saw `issued` and kept the claim, even though no POST had been made.
    Nothing was created and nothing would create it, but the URL stayed
    reserved for the whole stale-claim window and every other session skipped
    it. `issued` must mean what it says: a request has actually gone out.
    """
    db = tmp_path / "idx.db"
    init_db(db)

    def _boom(*a, **k):
        raise OSError("read-only state directory")

    monkeypatch.setattr(cap, "open_incident", _boom)
    zotero = _JournalRefuses()

    capture_message(
        message=f"see {URL}",
        project_slug="p",
        context=None,
        today=date(2026, 8, 26),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda u: "t",
        incident_id="inc-1",
        pinned_root="/c/NEW",
        running_root="/c/OLD",
        ledger_path=tmp_path / "health.db",
    )

    assert zotero.posted == [], "positive control: the write must not have happened"
    assert lookup_url(db, URL) is None, (
        "the URL is still reserved for a write that never went out"
    )


def test_run_capture_will_not_invent_a_ledger_path(tmp_path: Path) -> None:
    """0.20.2 made the caller own the path; the fallback made that a convention.

    A caller that forgets still gets a ledger -- next to the log, which is where
    the checker used to look and no longer does. An invariant the type system
    can hold should not be left to everyone remembering it.
    """
    from zotero_capture.cli import run_capture

    sig = inspect.signature(run_capture)
    assert sig.parameters["ledger_path"].default is inspect.Parameter.empty, (
        "ledger_path is still optional, so it can still be silently defaulted"
    )


def test_the_ledger_uses_a_bounded_busy_timeout() -> None:
    """SQLite's Python default is 5000 ms, and a Stop hook has 10000 ms total.

    Two journalled URLs contending with another session can therefore consume
    the entire hook budget before any work happens, and the hook's `timeout`
    kill leaves no record at all -- the ledger exists precisely so that a
    killed hook is still accounted for.
    """
    import sqlite3

    from zotero_capture.health_ledger import BUSY_TIMEOUT_MS, _connect

    assert 0 < BUSY_TIMEOUT_MS <= 2000, (
        f"{BUSY_TIMEOUT_MS} ms leaves too little of a 10 s hook budget"
    )
    conn = _connect(Path("/tmp/zp-timeout-probe.db"), create=True)
    try:
        assert conn is not None
        got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert got == BUSY_TIMEOUT_MS, f"the pragma was not applied: {got}"
    finally:
        if conn is not None:
            conn.close()
        Path("/tmp/zp-timeout-probe.db").unlink(missing_ok=True)


# --- the pin is authorisation, and it was read once for the whole capture ----


class _Records:
    def __init__(self) -> None:
        self.posted: list[str] = []

    def post_webpage_item(self, *, url_canonical, item_key=None, **kw):
        self.posted.append(url_canonical)
        return item_key or "KEY"

    def add_tags(self, *a, **k):
        pass

    def item_exists(self, key):
        return True


TWO = ["https://fixturehost.org/first", "https://fixturehost.org/second"]


def _capture(db: Path, ledger: Path, observer):
    zotero = _Records()
    capture_message(
        message=f"see {TWO[0]} and {TWO[1]}",
        project_slug="p",
        context=None,
        today=date(2026, 8, 26),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda u: "t",
        incident_id="inc-1",
        running_root="/c/SAME",
        observe_pin=observer,
        ledger_path=ledger,
    )
    return zotero


def test_a_pin_that_moves_mid_capture_is_seen_by_the_writes_after_it(
    tmp_path: Path,
) -> None:
    """The pin authorises the write, and it was read once for the whole message.

    An upgrade landing between two URLs of one capture left the second write
    running from a root the registry no longer pinned -- recorded as healthy,
    because the authorisation had been cached before it went stale. A capture
    can span several seconds of network I/O, which is ample.
    """
    from zotero_capture.health_ledger import open_incidents

    db = tmp_path / "idx.db"
    init_db(db)
    ledger = tmp_path / "health.db"
    seen = iter(["/c/SAME", "/c/NEW"])  # the pin moves after the first write

    zotero = _capture(db, ledger, lambda: next(seen, "/c/NEW"))

    assert zotero.posted == TWO, "positive control: both writes happened"
    found = open_incidents(ledger)
    assert [i["url"] for i in found] == [TWO[1]], (
        f"the write made after the pin moved was recorded as healthy: {found}"
    )
    assert found[0]["kind"] == "stale"


def test_a_pin_that_stays_put_records_nothing(tmp_path: Path) -> None:
    """Positive control: re-reading the pin must not invent incidents."""
    from zotero_capture.health_ledger import count_open

    db = tmp_path / "idx.db"
    init_db(db)
    ledger = tmp_path / "health.db"

    _capture(db, ledger, lambda: "/c/SAME")

    assert count_open(ledger) == 0


# --- legacy ids, and the double-import 0.21.0 introduced ---------------------


def _legacy_record(ts: str, root: str, urls_new: int = 1, **extra) -> str:
    import json

    return json.dumps({
        "ts": ts, "version": "0.15.0", "root": root, "pinned_root": "/c/NEW",
        "project": "p", "urls_seen": 1, "urls_new": urls_new,
        "urls_recurring": 0, "errors": [], **extra,
    })


def test_two_legacy_incidents_in_one_second_get_distinct_ids() -> None:
    """`legacy:<second>|<root>` is the aliasing 0.19.0 deleted, re-entering.

    Two writes from one root inside the same second collapsed onto one key, so
    acknowledging either silenced both -- permanently, and without the second
    ever being shown. Exactly the defect that made incidents carry a
    writer-issued uuid in the first place.
    """
    from zotero_capture.health import incidents

    lines = [
        _legacy_record("2026-08-25T11:40:00-0400", "/c/OLD", urls_new=1),
        _legacy_record("2026-08-25T11:40:00-0400", "/c/OLD", urls_new=2),
    ]

    found = incidents(lines, pinned_root="/c/NEW", require_id=False)

    assert len(found) == 2, f"positive control: both are incidents: {found}"
    assert len({i["id"] for i in found}) == 2, (
        f"two distinct incidents share one acknowledgement id: "
        f"{[i['id'] for i in found]}"
    )


def test_a_legacy_id_is_stable_across_reads() -> None:
    """The id is an acknowledgement handle; it cannot change between runs."""
    from zotero_capture.health import incidents

    lines = [_legacy_record("2026-08-25T11:40:00-0400", "/c/OLD")]
    first = incidents(lines, pinned_root="/c/NEW", require_id=False)[0]["id"]
    second = incidents(lines, pinned_root="/c/NEW", require_id=False)[0]["id"]

    assert first == second


def test_migration_does_not_re_import_a_record_that_journalled_itself(
    tmp_path: Path,
) -> None:
    """The double-count 0.21.0 introduced, named in its own CHANGELOG.

    While the ledger key WAS the capture id, re-importing a 0.19+ record hit
    ON CONFLICT DO NOTHING against the row that record had already written, and
    was dropped. That dedup was accidental. With per-mutation keys it no longer
    collides, so a log migrated after its own captures counts one capture twice.

    A record carrying an incident_id journalled itself when it ran; migration is
    for records that could not.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from zotero_capture_health import _migrate_legacy
    from zotero_capture.health_ledger import count_open

    state = tmp_path / "state"
    state.mkdir()
    (state / "capture.log").write_text(
        _legacy_record("2026-08-25T11:40:00-0400", "/c/OLD", incident_id="its-own")
        + "\n"
    )
    ledger = state / "health.db"

    _migrate_legacy(state, ledger, "/c/NEW")

    assert count_open(ledger) == 0, (
        "a record that already journalled itself was imported again"
    )


def test_migration_still_imports_a_record_that_could_not_journal_itself(
    tmp_path: Path,
) -> None:
    """Positive control: suppressing every open incident at upgrade is the bug
    this migration exists to prevent."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from zotero_capture_health import _migrate_legacy
    from zotero_capture.health_ledger import count_open

    state = tmp_path / "state"
    state.mkdir()
    (state / "capture.log").write_text(
        _legacy_record("2026-08-25T11:40:00-0400", "/c/OLD") + "\n"
    )
    ledger = state / "health.db"

    _migrate_legacy(state, ledger, "/c/NEW")

    assert count_open(ledger) == 1


# --- a torn append is not the same as a writer that stopped working ----------


def _good_record() -> str:
    import json

    return json.dumps({
        "ts": "2026-08-25T11:00:00-0400", "version": "x", "root": "/c/NEW",
        "pinned_root": "/c/NEW", "project": "p", "urls_seen": 1,
        "urls_new": 1, "urls_recurring": 0, "errors": [], "incident_id": "ok",
    })


def _check(lines):
    from datetime import datetime, timedelta, timezone

    from zotero_capture.health import evaluate

    tz = timezone(timedelta(hours=-4))
    return evaluate(
        lines, pinned_root="/c/NEW",
        now=datetime(2026, 8, 25, 12, 0, tzinfo=tz),
        window=timedelta(hours=24), ledger_path=None,
    )


def test_one_complete_unreadable_line_is_reported() -> None:
    """MIN_BROKEN_TAIL = 3 meant one or two junk lines never warned.

    A COMPLETE line -- newline-terminated -- that does not parse is not a torn
    append. The writer finished writing it and it is not a record, which is a
    writer fault however few of them there are.
    """
    warnings = _check([_good_record() + "\n", "Traceback (most recent call last):\n"])

    assert any("unreadable" in w or "readable" in w for w in warnings), warnings


def test_a_torn_final_append_is_not_reported() -> None:
    """The ordinary case: we read while the writer is mid-append.

    The last line has no newline yet. That is the evidence distinguishing it
    from a finished line that is junk, and `line.strip()` threw it away -- which
    is why the threshold had to be 3 in the first place.
    """
    warnings = _check([_good_record() + "\n", '{"ts": "2026-08-25T11:0'])

    assert not any("unreadable" in w or "readable" in w for w in warnings), warnings


def test_a_run_of_junk_is_still_reported_even_if_the_last_is_torn() -> None:
    """Positive control: exempting the final line must not exempt the run."""
    warnings = _check([
        _good_record() + "\n", "fatal\n", "fatal\n", '{"ts": "2026-08-2',
    ])

    assert any("unreadable" in w or "readable" in w for w in warnings), warnings
