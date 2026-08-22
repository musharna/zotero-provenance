"""A claim must resolve to exactly one item, whatever happened to the response.

The reservation makes `lookup -> POST -> insert` atomic across sessions: the row
is written first with an empty key, so a second session cannot also decide the
URL is new. That part was right. What it got wrong was assuming a POST that
raised had not created anything.

post_webpage_item can raise after Zotero has committed — while reading the
response, decoding it, or validating its shape. Releasing the claim then was
exactly wrong: the item existed, nothing in the index pointed at it, and the next
sighting POSTed a second copy. The opposite failure was just as silent: if the
index write after a successful POST failed, the claim stayed behind forever and
every later sighting skipped the URL.

Neither is fixable by a timeout alone, because an expired claim is ambiguous —
it may or may not already have an item. So the key is chosen BEFORE the POST and
recorded with the claim, which makes the question answerable: ask Zotero whether
that key exists. The Zotero API allows a client-supplied object key matching
/[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}/, and sending "version": 0 makes it a
versioned write, so no write token is needed.

Reported by an external audit of v0.10.0 (2026-08-22).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from zotero_capture.capture import STALE_CLAIM_S, capture_message
from zotero_capture.sqlite_cache import (
    init_db,
    lookup_url,
    queue_pending_tags,
    reserve_url,
    set_zotero_key,
    take_pending_tags,
)

URL = "https://fixturehost.org/contested"
TODAY = date(2026, 8, 22)


def _now() -> datetime:
    return datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


class FakeZotero:
    """Records what was asked of Zotero, and what Zotero actually holds."""

    def __init__(self, *, items: set[str] | None = None, fail_post: bool = False):
        self.items = set(items or ())
        self.fail_post = fail_post
        self.posts: list[str] = []
        self.existence_checks: list[str] = []
        self.tagged: list[tuple[str, list[str]]] = []

    def post_webpage_item(self, *, url_canonical, title, access_date, tags, item_key):
        self.posts.append(item_key)
        # The item is committed BEFORE the response is returned, which is the
        # whole point: raising here must not mean "nothing was created".
        self.items.add(item_key)
        if self.fail_post:
            raise RuntimeError("connection reset while reading the response")
        return item_key

    def item_exists(self, item_key: str) -> bool:
        self.existence_checks.append(item_key)
        return item_key in self.items

    def add_tags(self, item_key, new_tags, *, title_resolver=None, attempts=3):
        self.tagged.append((item_key, list(new_tags)))
        return True


def _capture(db, zotero, *, now=None, message=f"See {URL}"):
    return capture_message(
        message=message,
        project_slug="p",
        context="c",
        today=TODAY,
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda _u: "T",
        now=now or _now(),
    )


# --- the claim records which key it will use ---


def test_a_reservation_records_the_key_it_intends_to_create(tmp_path):
    db = tmp_path / "i.db"
    init_db(db)
    assert reserve_url(db, URL, TODAY, pending_key="ABCD2345", now=_now())
    row = lookup_url(db, URL)
    assert row is not None
    assert row["zotero_key"] == ""
    assert row["pending_key"] == "ABCD2345"
    assert row["claimed_at"]


def test_a_second_reservation_of_the_same_url_loses(tmp_path):
    """Positive control: the atomicity the claim exists for is unchanged."""
    db = tmp_path / "i.db"
    init_db(db)
    assert reserve_url(db, URL, TODAY, pending_key="ABCD2345", now=_now())
    assert not reserve_url(db, URL, TODAY, pending_key="EFGH6789", now=_now())


# --- a lost response must not become a duplicate ---


def test_a_post_that_committed_then_raised_is_not_posted_again(tmp_path):
    db = tmp_path / "i.db"
    init_db(db)
    z = FakeZotero(fail_post=True)
    _capture(db, z)
    assert len(z.posts) == 1, "the first run should have attempted exactly one POST"
    assert lookup_url(db, URL)["zotero_key"] == "", "claim released despite an item"

    later = _now() + timedelta(seconds=STALE_CLAIM_S + 1)
    z2 = FakeZotero(items=z.items)
    _capture(db, z2, now=later)

    assert z2.posts == [], "a committed item was posted a second time"
    assert z2.existence_checks == z.posts, "recovery never asked whether it existed"
    assert lookup_url(db, URL)["zotero_key"] == z.posts[0]


def test_a_stale_claim_with_no_item_is_released_and_retried(tmp_path):
    """The other half: if nothing was created, the URL must not be stranded."""
    db = tmp_path / "i.db"
    init_db(db)
    reserve_url(db, URL, TODAY, pending_key="ABCD2345", now=_now())

    later = _now() + timedelta(seconds=STALE_CLAIM_S + 1)
    z = FakeZotero()  # Zotero holds nothing
    result = _capture(db, z, now=later)

    assert z.existence_checks == ["ABCD2345"]
    assert len(z.posts) == 1, "a stale claim with no item should be retried"
    assert result.urls_new == 1
    assert lookup_url(db, URL)["zotero_key"] == z.posts[0]


def test_a_fresh_claim_from_another_session_is_left_alone(tmp_path):
    """Negative control: recovery must not interrupt a POST still in flight."""
    db = tmp_path / "i.db"
    init_db(db)
    reserve_url(db, URL, TODAY, pending_key="ABCD2345", now=_now())

    z = FakeZotero()
    _capture(db, z, now=_now() + timedelta(seconds=1))

    assert z.posts == [], "stole a claim that was still in flight"
    assert z.existence_checks == [], "questioned a claim that was still in flight"
    assert lookup_url(db, URL)["zotero_key"] == ""


# --- the loser keeps its provenance ---


def test_the_loser_of_a_race_queues_its_tags(tmp_path):
    db = tmp_path / "i.db"
    init_db(db)
    reserve_url(db, URL, TODAY, pending_key="ABCD2345", now=_now())

    z = FakeZotero()
    _capture(db, z, now=_now() + timedelta(seconds=1))

    queued = take_pending_tags(db, URL)
    assert "context:c" in queued
    assert "project:p" in queued


def test_queued_tags_are_applied_once_the_item_exists(tmp_path):
    db = tmp_path / "i.db"
    init_db(db)
    reserve_url(db, URL, TODAY, pending_key="ABCD2345", now=_now())
    queue_pending_tags(db, URL, ["context:lit-review", "project:other"])
    set_zotero_key(db, URL, "ABCD2345")

    z = FakeZotero(items={"ABCD2345"})
    _capture(db, z, now=_now() + timedelta(seconds=1))

    applied = [t for key, tags in z.tagged if key == "ABCD2345" for t in tags]
    assert "context:lit-review" in applied, "the losing session's context was lost"
    assert "project:other" in applied
    assert take_pending_tags(db, URL) == [], "queued tags were not cleared"


def test_taking_pending_tags_is_idempotent(tmp_path):
    """Draining twice must not re-apply, or every run would re-tag forever."""
    db = tmp_path / "i.db"
    init_db(db)
    queue_pending_tags(db, URL, ["context:x"])
    assert take_pending_tags(db, URL) == ["context:x"]
    assert take_pending_tags(db, URL) == []


@pytest.mark.parametrize("bad", ["", "abc", "ABCD234", "ABCD23451", "ABCD01AB"])
def test_a_generated_key_is_a_legal_zotero_key(bad):
    """The API only accepts /[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}/."""
    from zotero_capture.sqlite_cache import ZOTERO_KEY_RE, new_zotero_key

    assert not ZOTERO_KEY_RE.fullmatch(bad)
    for _ in range(50):
        assert ZOTERO_KEY_RE.fullmatch(new_zotero_key())
