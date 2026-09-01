"""Where a maintenance run got to, and what it destroyed on the way.

`retire` has journalled the rows it removes since it was written. Nothing else
did, and the gaps were not the same size:

  * `prune` trashes items with no record of which ones. The items sit in
    Zotero's trash so a human can get them back, but "which of these did that
    run put here" was unanswerable — which is the question you have when a pass
    surprises you.
  * `repair` REWRITES a URL, on the Zotero item and the index row, and the
    previous address was recorded nowhere at all. Not in a trash, not in a
    journal: it existed only in the in-memory plan and on stdout. That is the
    one maintenance path whose damage a human cannot undo.
  * and no run marked its own start or finish, so an interrupted pass left no
    way to ask where it stopped. The counts printed at the end are the only
    record, and an interrupted run never prints them.

The design turns on one distinction: a run whose BODY raises still writes its
end record, with the exception named, because a run that died is finished.
Only a process that never reached its handlers — SIGKILL, a power cut — leaves
an open run. Without that, every ordinary failure would be reported forever as
an unexplained interruption, which is the noise 0.15.0–0.18.0 kept having to cut
back out of the health check.
"""

from __future__ import annotations

from pathlib import Path

from zotero_capture.opjournal import (
    OperationJournal,
    journal_path,
    read_events,
    unfinished_operations,
)
from zotero_capture.prune import prune
from zotero_capture.repair import RepairStep, apply_repair

EXCLUDED = "https://evil.example.com/x"


def test_a_completed_run_is_not_unfinished(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    with OperationJournal(db, "prune") as journal:
        journal.outcome(journal.step(target="K1", action="trash"), "done")

    assert unfinished_operations(db) == []


def test_an_interrupted_run_is_reported_with_where_it_stopped(tmp_path: Path) -> None:
    """The defect this exists for. No end record means the process never got to
    run its handlers at all."""
    db = tmp_path / "index.db"
    journal = OperationJournal(db, "prune")
    journal.__enter__()
    journal.step(target="K1", action="trash", before={"url": EXCLUDED})
    journal.step(target="K2", action="trash", before={"url": "https://x.test/y"})
    # and then the process dies, with no __exit__

    open_ops = unfinished_operations(db)

    assert len(open_ops) == 1
    assert open_ops[0]["command"] == "prune"
    assert open_ops[0]["steps"] == 2
    assert open_ops[0]["last_target"] == "K2"


def test_a_run_whose_body_raised_is_finished(tmp_path: Path) -> None:
    """The distinction the whole design turns on. A run that died IS finished;
    reporting it as an unexplained interruption forever would turn every
    ordinary failure into permanent noise."""
    db = tmp_path / "index.db"
    try:
        with OperationJournal(db, "repair") as journal:
            journal.step(target="K1", action="rewrite")
            raise RuntimeError("network went away")
    except RuntimeError:
        pass

    assert unfinished_operations(db) == []
    end = [e for e in read_events(db) if e["event"] == "end"][0]
    assert "RuntimeError: network went away" in end["failed"]


def test_a_torn_final_line_does_not_hide_the_rest(tmp_path: Path) -> None:
    """A torn write is what an interruption looks like. Skipping the bad line is
    right; treating the whole journal as unreadable is not."""
    db = tmp_path / "index.db"
    journal = OperationJournal(db, "prune")
    journal.__enter__()
    journal.step(target="K1", action="trash")
    with journal_path(db).open("a", encoding="utf-8") as fh:
        fh.write('{"event": "step", "target": "K2"')  # no newline, no close brace

    open_ops = unfinished_operations(db)

    assert len(open_ops) == 1 and open_ops[0]["steps"] == 1


def test_the_journal_records_what_is_about_to_be_destroyed(tmp_path: Path) -> None:
    """`before` is what makes this a journal rather than a log."""
    db = tmp_path / "index.db"
    with OperationJournal(db, "repair") as journal:
        journal.step(
            target="K1", action="rewrite", before={"url": EXCLUDED, "corrected": "x"}
        )

    step = [e for e in read_events(db) if e["event"] == "step"][0]
    assert step["before"]["url"] == EXCLUDED


# --- wired into the destructive paths ---------------------------------------


class _Zotero:
    def __init__(self, *, trash_ok: bool = True) -> None:
        self.trash_ok = trash_ok

    def iter_collection_items(self):
        yield {"key": "ITEM1234", "data": {"url": EXCLUDED}}

    def trash_item(self, key, *, expect_url=None):
        return self.trash_ok


def test_prune_journals_each_trash_before_it_happens(tmp_path: Path) -> None:
    db = tmp_path / "index.db"

    with OperationJournal(db, "prune") as journal:
        prune(_Zotero(), sleep_s=0, journal=journal)

    events = read_events(db)
    step = [e for e in events if e["event"] == "step"][0]
    outcome = [e for e in events if e["event"] == "outcome"][0]
    assert step["before"]["url"] == EXCLUDED
    assert step["action"] == "trash"
    assert outcome["state"] == "done"


def test_prune_journals_a_refusal_as_a_refusal(tmp_path: Path) -> None:
    db = tmp_path / "index.db"

    with OperationJournal(db, "prune") as journal:
        prune(_Zotero(trash_ok=False), sleep_s=0, journal=journal)

    outcome = [e for e in read_events(db) if e["event"] == "outcome"][0]
    assert outcome["state"] == "refused"


def test_a_dry_run_journals_no_steps(tmp_path: Path) -> None:
    """Positive control for the two above: a run that destroys nothing must not
    fill the journal, or the runs that did destroy something get buried."""
    db = tmp_path / "index.db"

    with OperationJournal(db, "prune") as journal:
        prune(_Zotero(), dry_run=True, sleep_s=0, journal=journal)

    assert [e for e in read_events(db) if e["event"] == "step"] == []


def test_repair_journals_the_url_it_is_about_to_overwrite(tmp_path: Path) -> None:
    """The irreplaceable datum. Once the rewrite lands, the previous address
    exists nowhere else."""
    import sqlite3
    from contextlib import closing, contextmanager

    from zotero_capture.sqlite_cache import init_db, insert_url
    from datetime import date

    db = tmp_path / "index.db"
    init_db(db)
    old = "https://fixturehost.org/a**"
    insert_url(db, old, "K1", date(2026, 5, 5))

    @contextmanager
    def connect(path):
        with closing(sqlite3.connect(path)) as conn:
            yield conn
            conn.commit()

    class _Repairer:
        def _patch_item_url(self, key, corrected, *, expect_url=None):
            return True

    steps = [
        RepairStep(
            url=old,
            corrected="https://fixturehost.org/a",
            zotero_key="K1",
            action="rewrite",
        )
    ]
    apply_repair(steps, db_path=db, zotero=_Repairer(), connect=connect)

    step = [e for e in read_events(db) if e["event"] == "step"][0]
    assert step["before"]["url"] == old, "the overwritten URL was not recorded"
    assert step["command"] == "repair"
    outcome = [e for e in read_events(db) if e["event"] == "outcome"][0]
    assert outcome["state"] == "done"
