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


# --- the claim clock: a claim is stamped when it is taken, not when the run began ---


URL_FIRST = "https://fixturehost.org/slow-title"
URL_SECOND = "https://fixturehost.org/claimed-late"


class _FakeClock:
    """Wall time that only moves when the test says so."""

    def __init__(self, start: datetime):
        self.t = start

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)

    def datetime_class(self):
        clock = self

        class _ClockedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.t if tz is not None else clock.t.replace(tzinfo=None)

        return _ClockedDatetime


class _UrlRecordingZotero(FakeZotero):
    """One Zotero shared by two sessions, remembering which URL each POST was for."""

    def __init__(self):
        super().__init__()
        self.posts_by_url: list[tuple[str, str]] = []

    def post_webpage_item(self, *, url_canonical, title, access_date, tags, item_key):
        self.posts_by_url.append((url_canonical, item_key))
        return super().post_webpage_item(
            url_canonical=url_canonical,
            title=title,
            access_date=access_date,
            tags=tags,
            item_key=item_key,
        )


def test_a_late_claim_is_not_judged_stale_by_a_concurrent_session(tmp_path, monkeypatch):
    """The detached worker runs URLs one after another for up to its deadline.

    Taking `now` once per message stamped every claim with the run's START, so a
    URL reserved 70 s into the run was born 70 s old. A second session citing it
    at that moment saw a stale claim, asked Zotero for a key that had not been
    POSTed yet, released the claim, and POSTed its own item: one URL, two items,
    and the first session's key lost to dedup. Driven through the production
    path, which passes no `now`.
    """
    import zotero_capture.capture as capture_mod

    clock = _FakeClock(_now())
    monkeypatch.setattr(capture_mod, "datetime", clock.datetime_class())

    db = tmp_path / "i.db"
    init_db(db)
    zotero = _UrlRecordingZotero()
    seen: dict[str, object] = {}

    def concurrent_session_cites_second_url() -> None:
        row = lookup_url(db, URL_SECOND)
        seen["claimed_at"] = row["claimed_at"] if row else None
        seen["clock_at_reservation"] = clock.t
        capture_message(
            message=f"Also {URL_SECOND}",
            project_slug="other",
            context="c",
            today=TODAY,
            db_path=db,
            zotero=zotero,
            title_fetcher=lambda _u: "T-other",
        )
        seen["row_after_concurrent"] = lookup_url(db, URL_SECOND)

    def title_fetcher(url: str) -> str:
        if url == URL_FIRST:
            clock.advance(70)  # a slow title fetch, well inside the 120 s deadline
        elif url == URL_SECOND and "claimed_at" not in seen:
            concurrent_session_cites_second_url()
        return "T"

    result = capture_message(
        message=f"See {URL_FIRST} and {URL_SECOND}",
        project_slug="p",
        context="c",
        today=TODAY,
        db_path=db,
        zotero=zotero,
        title_fetcher=title_fetcher,
    )

    claimed_at = datetime.fromisoformat(seen["claimed_at"])
    assert claimed_at >= seen["clock_at_reservation"], (
        f"URL2 was claimed at {seen['clock_at_reservation']} but stamped "
        f"{claimed_at}: the claim was born "
        f"{(seen['clock_at_reservation'] - claimed_at).total_seconds():.0f} s old"
    )
    assert zotero.existence_checks == [], (
        f"a claim taken moments ago was questioned as stale: {zotero.existence_checks}"
    )
    second_posts = [k for u, k in zotero.posts_by_url if u == URL_SECOND]
    assert len(second_posts) == 1, f"URL2 was POSTed {len(second_posts)} times: {second_posts}"
    assert [e.code for e in result.errors] == [], result.errors
    # Positive control: the first session completed both URLs with its own keys.
    assert result.urls_new == 2
    assert lookup_url(db, URL_SECOND)["zotero_key"] == second_posts[0]
    assert seen["row_after_concurrent"]["zotero_key"] == ""  # still A's, unfinished


def test_a_live_claim_outlasts_the_workers_own_deadline():
    """STALE_CLAIM_S must exceed the longest a live worker can hold a claim.

    The comment once bounded it by the hook's 10 s timeout. Capture moved into a
    detached worker bounded by WORKER_DEADLINE_S, and the 60 s window fell inside
    it, so a claim could be judged abandoned while its owner was still running.
    """
    from zotero_capture.cli import WORKER_DEADLINE_S

    assert STALE_CLAIM_S > WORKER_DEADLINE_S, (STALE_CLAIM_S, WORKER_DEADLINE_S)
