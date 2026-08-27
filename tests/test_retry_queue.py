"""A failed write is no longer lost, and the queue that holds it cannot grow forever.

Capture had two recovery paths and both needed the URL to be cited AGAIN: an
unissued claim is released so a later run can retry, and an issued one is settled
by `_resolve_claim` on the next citation. A source cited once, which failed once,
was simply gone — silently, out of a library whose entire job is to record what
was actually consulted.

The README named the reason there was no queue: "a queue nothing drains cannot
grow forever". That reasoning is right, so the queue ships with its drain and
with two bounds — a full queue refuses new entries loudly rather than evicting
silently, and an entry that has failed `RETRY_MAX_ATTEMPTS` times is given up on
rather than retried until the end of time.

The drain issues no write of its own. It replays through `capture_message`, so
it inherits the reservation, the claim resolution and the dedup that already
answer "did that POST commit?" — re-posting blind is how duplicates are made.
And it replays under the ORIGINAL sighting date, because filing a source under
the day the retry ran rather than the day it was cited destroys the one fact the
library exists to record.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zotero_capture.capture import capture_message
from zotero_capture.drain import drain, format_drain_report
from zotero_capture.sqlite_cache import (
    RETRY_MAX_ATTEMPTS,
    RETRY_QUEUE_MAX,
    dequeue_retry,
    enqueue_retry,
    init_db,
    lookup_url,
    retry_queue_depth,
    retry_queue_entries,
)
from zotero_capture.zotero_client import ZoteroError

URL = "https://fixturehost.org/foo"
OTHER = "https://fixturehost.org/bar"


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


@pytest.fixture
def fake_zotero():
    z = MagicMock()
    z.post_webpage_item.return_value = "NEWKEY"
    z.add_tags.return_value = True
    return z


def _enqueue(db: Path, url: str = URL, *, now: str = "2026-08-27T10:00:00+00:00"):
    return enqueue_retry(
        db,
        url_canonical=url,
        project="home",
        context=None,
        seen_date="2026-08-20",
        error="simulated 500",
        now=now,
    )


# --- the queue --------------------------------------------------------------


def test_an_entry_is_remembered(db: Path) -> None:
    assert _enqueue(db) is True
    assert retry_queue_depth(db) == 1


def test_requeueing_counts_an_attempt_instead_of_adding_a_row(db: Path) -> None:
    """A URL failing every day must not crowd out the rest of the queue."""
    _enqueue(db)
    _enqueue(db, now="2026-08-28T10:00:00+00:00")

    entries = retry_queue_entries(db)
    assert len(entries) == 1
    assert entries[0]["attempts"] == 2
    assert entries[0]["first_failed"].startswith("2026-08-27")
    assert entries[0]["last_failed"].startswith("2026-08-28")


def test_a_full_queue_refuses_rather_than_evicting(db: Path) -> None:
    """The bound. Evicting silently would lose the oldest failure, which is the
    one most likely never to be cited again."""
    for i in range(RETRY_QUEUE_MAX):
        assert _enqueue(db, f"https://fixturehost.org/{i}") is True

    assert _enqueue(db, "https://fixturehost.org/one-too-many") is False
    assert retry_queue_depth(db) == RETRY_QUEUE_MAX


def test_dequeue_reports_whether_there_was_anything_to_remove(db: Path) -> None:
    _enqueue(db)
    assert dequeue_retry(db, URL) is True
    assert dequeue_retry(db, URL) is False
    assert retry_queue_depth(db) == 0


def test_the_oldest_failure_comes_first(db: Path) -> None:
    _enqueue(db, OTHER, now="2026-08-01T00:00:00+00:00")
    _enqueue(db, URL, now="2026-08-27T00:00:00+00:00")

    assert [e["url_canonical"] for e in retry_queue_entries(db)] == [OTHER, URL]


def test_an_index_without_the_table_reports_an_empty_queue(tmp_path: Path) -> None:
    """A legacy index predating the table is not a fault; nothing is queued."""
    import sqlite3

    legacy = tmp_path / "legacy.db"
    sqlite3.connect(legacy).close()

    assert retry_queue_depth(legacy) == 0


# --- capture puts things in it ---------------------------------------------


def test_a_failed_write_is_queued(db: Path, fake_zotero) -> None:
    fake_zotero.post_webpage_item.side_effect = ZoteroError("simulated 500")

    capture_message(
        message=f"See {URL}",
        project_slug="my-project",
        context="lit-review",
        today=date(2026, 5, 5),
        db_path=db,
        zotero=fake_zotero,
        title_fetcher=lambda url: "T",
    )

    entries = retry_queue_entries(db)
    assert [e["url_canonical"] for e in entries] == [URL]
    assert entries[0]["project"] == "my-project"
    assert entries[0]["context"] == "lit-review"
    assert entries[0]["seen_date"] == "2026-05-05", (
        "the sighting date is the provenance and must survive the retry"
    )


def test_a_successful_write_queues_nothing(db: Path, fake_zotero) -> None:
    """Positive control. Without it, a queue that swallowed everything would
    satisfy the test above."""
    capture_message(
        message=f"See {URL}",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=fake_zotero,
        title_fetcher=lambda url: "T",
    )

    assert retry_queue_depth(db) == 0


def test_overflow_is_reported_rather_than_swallowed(db: Path, fake_zotero) -> None:
    """Dropping the overflow quietly would rebuild "logged and dropped" one
    level up, which is the behaviour the queue exists to replace."""
    for i in range(RETRY_QUEUE_MAX):
        _enqueue(db, f"https://fixturehost.org/{i}")
    fake_zotero.post_webpage_item.side_effect = ZoteroError("simulated 500")

    result = capture_message(
        message=f"See {URL}",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=fake_zotero,
        title_fetcher=lambda url: "T",
    )

    assert [e.code for e in result.errors] == ["zotero_error", "retry_queue_full"]


# --- the drain --------------------------------------------------------------


class _Outcome:
    def __init__(self, errors=()) -> None:
        self.errors = list(errors)


class _Failure:
    def __init__(self, url: str) -> None:
        self.url = url


def test_a_recovered_entry_leaves_the_queue(db: Path) -> None:
    _enqueue(db)

    result = drain(db, replay=lambda entry: _Outcome())

    assert result.recovered_urls == [URL]
    assert retry_queue_depth(db) == 0


def test_an_entry_that_fails_again_stays_queued(db: Path) -> None:
    _enqueue(db)

    result = drain(db, replay=lambda entry: _Outcome([_Failure(URL)]))

    assert result.recovered_urls == []
    assert result.still_failing_urls == [URL]
    assert retry_queue_depth(db) == 1, "a still-failing entry must not be dropped"


def test_a_replay_that_raises_leaves_the_entry_alone(db: Path) -> None:
    def boom(entry):
        raise RuntimeError("network down")

    result = drain(db, replay=boom) if _enqueue(db) else None

    assert result is not None
    assert result.still_failing_urls == [URL]
    assert retry_queue_depth(db) == 1


def test_an_entry_is_given_up_on_after_the_attempt_limit(db: Path) -> None:
    """The other bound. Retrying forever is what a bounded queue prevents."""
    for i in range(RETRY_MAX_ATTEMPTS):
        _enqueue(db, now=f"2026-08-{20 + i:02d}T00:00:00+00:00")
    assert retry_queue_entries(db)[0]["attempts"] == RETRY_MAX_ATTEMPTS

    replayed: list[str] = []
    result = drain(db, replay=lambda entry: replayed.append(entry["url_canonical"]))

    assert replayed == [], "an abandoned entry must not be retried again"
    assert result.abandoned_urls == [URL]
    assert retry_queue_depth(db) == 0


def test_a_dry_run_changes_nothing(db: Path) -> None:
    _enqueue(db)

    def _unreachable(entry):
        raise AssertionError("a dry run must not replay")

    result = drain(db, replay=_unreachable, dry_run=True)

    assert result.would_retry == 1
    assert retry_queue_depth(db) == 1


def test_the_report_does_not_claim_a_still_failing_url_was_recovered(db: Path) -> None:
    """The Wave 2 lesson, applied before it can happen again."""
    _enqueue(db)

    result = drain(db, replay=lambda entry: _Outcome([_Failure(URL)]))
    body = "\n".join(format_drain_report(result, dry_run=False))

    assert f"recovered: {URL}" not in body, body
    assert f"still failing: {URL}" in body, body


def test_the_report_names_what_was_recovered(db: Path) -> None:
    """Positive control for the report test above."""
    _enqueue(db)

    result = drain(db, replay=lambda entry: _Outcome())
    body = "\n".join(format_drain_report(result, dry_run=False))

    assert f"recovered: {URL}" in body, body


def test_a_replayed_url_is_not_double_posted(db: Path, fake_zotero) -> None:
    """The drain replays through capture_message, so dedup applies to it too.

    A queue that re-issued its own POST would create a second item for a URL
    whose first POST had in fact committed.
    """
    capture_message(
        message=f"See {URL}",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=fake_zotero,
        title_fetcher=lambda url: "T",
    )
    assert fake_zotero.post_webpage_item.call_count == 1
    _enqueue(db)  # as if the write had been recorded as failed

    drain(
        db,
        replay=lambda entry: capture_message(
            message=entry["url_canonical"],
            project_slug=entry["project"],
            context=entry["context"],
            today=date.fromisoformat(entry["seen_date"]),
            db_path=db,
            zotero=fake_zotero,
            title_fetcher=lambda url: "T",
        ),
    )

    assert fake_zotero.post_webpage_item.call_count == 1, "the drain posted again"
    assert lookup_url(db, URL)["zotero_key"] == "NEWKEY"
    assert retry_queue_depth(db) == 0
