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
