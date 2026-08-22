"""capture pipeline tests — orchestrates extract→exclude→dedup→POST/PATCH per spec §4."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zotero_capture.capture import (
    MAX_REENRICH_PER_RUN,
    CaptureResult,
    capture_message,
)
from zotero_capture.sqlite_cache import init_db, insert_url


@pytest.fixture
def empty_cache(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


@pytest.fixture
def fake_zotero():
    z = MagicMock()
    z.post_webpage_item.return_value = "NEWKEY"
    z.add_tags.return_value = True
    return z


@pytest.fixture
def fake_title_fetcher():
    def _fetcher(url: str) -> str:
        return f"Title-of-{url}"

    return _fetcher


def test_capture_new_url_posts_to_zotero(empty_cache, fake_zotero, fake_title_fetcher):
    result = capture_message(
        message="See https://fixturehost.org/foo for details.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    fake_zotero.post_webpage_item.assert_called_once()
    call = fake_zotero.post_webpage_item.call_args.kwargs
    assert call["url_canonical"] == "https://fixturehost.org/foo"
    assert call["access_date"] == "2026-05-05"
    assert "context:general" in call["tags"]
    assert "project:home" in call["tags"]
    assert "seen:2026-05-05" in call["tags"]
    assert "domain:fixturehost.org" in call["tags"]
    assert result.urls_new == 1


def test_capture_known_url_same_day_same_context_is_noop(
    empty_cache, fake_zotero, fake_title_fetcher
):
    insert_url(
        empty_cache,
        "https://fixturehost.org/foo",
        "EXISTKEY",
        first_seen=date(2026, 5, 5),
    )
    fake_zotero.add_tags.return_value = False
    result = capture_message(
        message="https://fixturehost.org/foo",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    fake_zotero.post_webpage_item.assert_not_called()
    assert result.urls_new == 0
    assert result.urls_recurring == 1


def test_capture_known_url_new_context_adds_tag(
    empty_cache, fake_zotero, fake_title_fetcher
):
    insert_url(
        empty_cache,
        "https://fixturehost.org/foo",
        "EXISTKEY",
        first_seen=date(2026, 5, 1),
    )
    result = capture_message(
        message="Source: https://fixturehost.org/foo",
        project_slug="home",
        context="lit-review",
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    fake_zotero.add_tags.assert_called_once()
    new_tags = fake_zotero.add_tags.call_args.args[1]
    assert "context:lit-review" in new_tags
    assert "seen:2026-05-05" in new_tags
    assert result.urls_recurring == 1


def test_capture_excludes_localhost(empty_cache, fake_zotero, fake_title_fetcher):
    result = capture_message(
        message="See http://localhost:3000/foo and https://fixturehost.org/bar",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert result.urls_excluded == 1
    assert result.urls_new == 1


def test_capture_returns_zero_on_empty_message(
    empty_cache, fake_zotero, fake_title_fetcher
):
    result = capture_message(
        message="nothing here",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    fake_zotero.post_webpage_item.assert_not_called()
    fake_zotero.add_tags.assert_not_called()
    assert result == CaptureResult(
        urls_seen=0, urls_new=0, urls_recurring=0, urls_excluded=0, errors=[]
    )


def test_capture_dedups_canonical_collisions(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """Two raw URLs that canonicalize to the same string → one POST, urls_seen==1."""
    result = capture_message(
        message="See https://fixturehost.org/ and https://fixturehost.org for details",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert fake_zotero.post_webpage_item.call_count == 1
    assert result.urls_seen == 1
    assert result.urls_new == 1


def test_capture_zotero_error_on_post_records_error_no_insert(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """ZoteroError on POST → urls_new stays 0, error recorded, no completed row.

    The row itself now survives as an unfulfilled claim. A 500 is returned by a
    server that may already have committed the write, so deleting the claim
    would throw away the only record of which key to ask about — which is how
    the same URL got posted twice. It is settled later by _resolve_claim.
    """
    from zotero_capture.sqlite_cache import lookup_url
    from zotero_capture.zotero_client import ZoteroError

    fake_zotero.post_webpage_item.side_effect = ZoteroError("simulated 500")
    result = capture_message(
        message="See https://fixturehost.org/foo",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert result.urls_new == 0
    assert len(result.errors) == 1
    assert result.errors[0].url == "https://fixturehost.org/foo"
    assert result.errors[0].code == "zotero_error"
    row = lookup_url(empty_cache, "https://fixturehost.org/foo")
    assert row is not None and row["zotero_key"] == "", "no item may be recorded"


def test_capture_zotero_error_on_add_tags_records_error_no_last_seen_update(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """ZoteroError on add_tags → urls_recurring stays 0, error recorded, last_seen unchanged."""
    from zotero_capture.sqlite_cache import lookup_url
    from zotero_capture.zotero_client import ZoteroError

    insert_url(
        empty_cache,
        "https://fixturehost.org/foo",
        "EXISTKEY",
        first_seen=date(2026, 5, 1),
    )
    fake_zotero.add_tags.side_effect = ZoteroError("simulated 503")
    result = capture_message(
        message="https://fixturehost.org/foo",
        project_slug="home",
        context="lit-review",
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert result.urls_recurring == 0
    assert len(result.errors) == 1
    row = lookup_url(empty_cache, "https://fixturehost.org/foo")
    assert row is not None
    assert row["last_seen"] == "2026-05-01"  # unchanged


def test_new_url_with_unfetchable_title_is_tagged_unresolved(empty_cache, fake_zotero):
    """A failed title fetch must be MARKED, not silently stored as if it were a title."""
    capture_message(
        message="https://fixturehost.org/foo",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=lambda url: url,  # fetch failed -> URL-as-fallback sentinel
    )
    tags = fake_zotero.post_webpage_item.call_args.kwargs["tags"]
    assert "title:unresolved" in tags


def test_new_url_with_real_title_is_not_tagged_unresolved(
    empty_cache, fake_zotero, fake_title_fetcher
):
    capture_message(
        message="https://fixturehost.org/foo",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    tags = fake_zotero.post_webpage_item.call_args.kwargs["tags"]
    assert "title:unresolved" not in tags


def test_recurring_url_is_offered_a_title_resolver(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """Recurrence is the retry opportunity: the client must be handed a way to resolve."""
    insert_url(
        empty_cache,
        "https://fixturehost.org/foo",
        "EXISTKEY",
        first_seen=date(2026, 5, 1),
    )
    capture_message(
        message="https://fixturehost.org/foo",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    resolver = fake_zotero.add_tags.call_args.kwargs["title_resolver"]
    assert resolver is not None
    assert resolver() == "Title-of-https://fixturehost.org/foo"


def test_reenrichment_is_capped_per_run(empty_cache, fake_zotero):
    """Many recurring unfetchable URLs must not blow the Stop hook's time budget."""
    fetched: list[str] = []

    def counting_fetcher(url: str) -> str:
        fetched.append(url)
        return f"Title-of-{url}"

    # Simulate every recurring item still having an unresolved title.
    def add_tags(key, tags, *, title_resolver=None):
        if title_resolver is not None:
            title_resolver()
        return True

    fake_zotero.add_tags.side_effect = add_tags

    urls = [f"https://fixturehost.org/{i}" for i in range(MAX_REENRICH_PER_RUN + 2)]
    for i, u in enumerate(urls):
        insert_url(empty_cache, u, f"KEY{i}", first_seen=date(2026, 5, 1))

    capture_message(
        message=" ".join(urls),
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=counting_fetcher,
    )
    assert len(fetched) == MAX_REENRICH_PER_RUN


def test_a_message_marked_as_plugin_output_captures_nothing(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """Second, independent layer against the plugin re-capturing its own report.

    The backtick rule already covers the URLs themselves; this covers a report
    that got reformatted on the way out, which is the failure mode a rule based
    purely on formatting cannot survive alone.
    """
    from zotero_capture.capture import NO_CAPTURE_MARKER

    result = capture_message(
        message=f"{NO_CAPTURE_MARKER}\n\nSee https://fixturehost.org/reported here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )

    assert result.urls_seen == 0
    fake_zotero.post_webpage_item.assert_not_called()


def test_the_same_message_without_the_marker_is_captured(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """Positive control: the marker must be doing the work, not the prose."""
    result = capture_message(
        message="See https://fixturehost.org/reported here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )

    assert result.urls_seen == 1
    fake_zotero.post_webpage_item.assert_called_once()


def test_a_url_another_session_is_mid_post_on_is_not_posted_again(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """The race the reservation exists to stop.

    A reservation with no key means another process has claimed the URL and its
    POST is in flight. Creating a second item here is what orphaned one of them:
    only one key fits in the index, so the other Zotero item became invisible to
    dedup permanently.
    """
    from zotero_capture.sqlite_cache import reserve_url

    reserve_url(empty_cache, "https://fixturehost.org/contested", date(2026, 5, 5))

    result = capture_message(
        message="See https://fixturehost.org/contested here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )

    fake_zotero.post_webpage_item.assert_not_called()
    # And it must not "tag" the empty key either: the old code fell straight
    # through to the recurring branch and PATCHed an item key of "".
    fake_zotero.add_tags.assert_not_called()
    assert result.urls_new == 0


def test_the_claim_is_taken_before_the_item_is_created(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """The ordering that makes dedup safe, observed from inside the POST.

    Asserting only the end state cannot tell the fixed code from the broken one:
    both leave exactly one row behind. What changed is WHEN the row appears —
    before the network call, so a second process cannot also conclude the URL is
    new while this POST is still in flight.
    """
    from zotero_capture.sqlite_cache import lookup_url

    observed: dict = {}

    def _post(**kwargs):
        observed["row"] = lookup_url(empty_cache, "https://fixturehost.org/ordered")
        return "NEWKEY"

    fake_zotero.post_webpage_item.side_effect = _post
    capture_message(
        message="See https://fixturehost.org/ordered here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )

    assert observed["row"] is not None, "the URL must be claimed before the POST"
    assert observed["row"]["zotero_key"] == "", "and the key only lands afterwards"
    row = lookup_url(empty_cache, "https://fixturehost.org/ordered")
    assert row["zotero_key"] == "NEWKEY"


def test_a_failure_before_the_request_releases_the_reservation(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """A claim that provably produced no item must not block the URL forever.

    This asserts a narrower thing than it used to. It once covered a failing
    POST as well, on the assumption that a POST which raised had created
    nothing — which is false for a network call, and releasing on that
    assumption is what produced duplicate items. Releasing is now confined to
    failures that happen before the request goes out, where "nothing was
    created" is actually known. The ambiguous case is settled by asking Zotero;
    see tests/test_reservation_recovery.py.
    """
    from zotero_capture.sqlite_cache import lookup_url

    def _boom(_url: str) -> str:
        raise RuntimeError("title fetch failed before anything was sent")

    capture_message(
        message="See https://fixturehost.org/doomed here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=_boom,
    )
    assert fake_zotero.post_webpage_item.call_count == 0, (
        "nothing should have been sent"
    )
    assert lookup_url(empty_cache, "https://fixturehost.org/doomed") is None

    # Positive control: the retry actually succeeds once the fetcher recovers.
    result = capture_message(
        message="See https://fixturehost.org/doomed here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert result.urls_new == 1
    assert (
        lookup_url(empty_cache, "https://fixturehost.org/doomed")["zotero_key"]
        == "NEWKEY"
    )


def test_a_failed_post_keeps_the_reservation_for_recovery(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """Once the request is out, "it raised" says nothing about what Zotero did."""
    from zotero_capture.sqlite_cache import lookup_url

    fake_zotero.post_webpage_item.side_effect = RuntimeError("connection reset")
    capture_message(
        message="See https://fixturehost.org/doomed here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    row = lookup_url(empty_cache, "https://fixturehost.org/doomed")
    assert row is not None, "the claim was dropped while an item might exist"
    assert row["zotero_key"] == ""
    assert row["pending_key"], "recovery needs the key the POST was sent under"
