"""Reservation-protocol defects from the 2026-08-25 audit.

Both are about identity: who owns a claim, and who owns the provenance queued
against it while it was still in flight.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zotero_capture.capture import capture_message
from zotero_capture.sqlite_cache import (
    init_db,
    lookup_url,
    peek_pending_tags,
    queue_pending_tags,
    release_url,
    reserve_url,
    set_zotero_key,
)

URL = "https://fixturehost.org/contested"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "url_index.db"
    init_db(p)
    return p


@pytest.fixture
def zotero():
    z = MagicMock()
    z.post_webpage_item.return_value = "NEWKEY"
    z.add_tags.return_value = True
    return z


def test_completing_a_claim_applies_tags_another_session_queued(db, zotero):
    """The half the peek/clear fix did not reach.

    B sees A's in-flight claim, has real provenance and no item to put it on, so
    it queues. A then completes the item and moves on without ever looking — so
    B's sighting waits for a recurrence that may never come. If the URL is cited
    once, by two sessions at once, one session's record of it is simply absent.
    """
    queue_pending_tags(db, URL, ["context:from-B", "project:B"])
    capture_message(
        message=f"See {URL} here.",
        project_slug="A",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda u: "T",
    )
    posted = zotero.post_webpage_item.call_args.kwargs["tags"]
    applied = posted + (
        list(zotero.add_tags.call_args.args[1]) if zotero.add_tags.called else []
    )
    assert "context:from-B" in applied and "project:B" in applied, (
        f"the other session's provenance was never applied: {applied}"
    )
    assert peek_pending_tags(db, URL) == [], "and it must not be left queued"


def test_a_failed_completion_leaves_the_queued_tags_alone(db, zotero):
    """Positive control's mirror: a failure must not consume them either."""
    from zotero_capture.zotero_client import ZoteroError

    queue_pending_tags(db, URL, ["context:from-B"])
    zotero.post_webpage_item.side_effect = ZoteroError("503")
    capture_message(
        message=f"See {URL} here.",
        project_slug="A",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda u: "T",
    )
    assert peek_pending_tags(db, URL) == ["context:from-B"]


def test_release_does_not_drop_a_successor_claim(db):
    """A stalled owner must not release the claim that replaced it.

    A claims, stalls. B judges A abandoned, releases it and claims with its own
    key. A wakes, fails before its POST, and releases — matching on URL alone,
    so it destroys B's live claim and B's POST becomes an orphan item no dedup
    can ever see.
    """
    reserve_url(db, URL, date(2026, 5, 5), pending_key="AAAAAAAA")
    release_url(db, URL, pending_key="AAAAAAAA")
    reserve_url(db, URL, date(2026, 5, 5), pending_key="BBBBBBBB")

    release_url(db, URL, pending_key="AAAAAAAA")  # A wakes up late

    row = lookup_url(db, URL)
    assert row is not None, "B's claim was destroyed by the previous owner"
    assert row["pending_key"] == "BBBBBBBB"


def test_completion_does_not_overwrite_a_successor_key(db):
    """The same race on the other side: A must not stamp its key onto B's row."""
    reserve_url(db, URL, date(2026, 5, 5), pending_key="AAAAAAAA")
    release_url(db, URL, pending_key="AAAAAAAA")
    reserve_url(db, URL, date(2026, 5, 5), pending_key="BBBBBBBB")
    set_zotero_key(db, URL, "BBBBBBBB", pending_key="BBBBBBBB")

    set_zotero_key(db, URL, "AAAAAAAA", pending_key="AAAAAAAA")  # A wakes up late

    assert lookup_url(db, URL)["zotero_key"] == "BBBBBBBB"


def test_the_rightful_owner_can_still_complete_and_release(db):
    """Positive control: CAS must not break the ordinary path."""
    reserve_url(db, URL, date(2026, 5, 5), pending_key="CCCCCCCC")
    set_zotero_key(db, URL, "CCCCCCCC", pending_key="CCCCCCCC")
    assert lookup_url(db, URL)["zotero_key"] == "CCCCCCCC"

    other = "https://fixturehost.org/other"
    reserve_url(db, other, date(2026, 5, 5), pending_key="DDDDDDDD")
    release_url(db, other, pending_key="DDDDDDDD")
    assert lookup_url(db, other) is None
