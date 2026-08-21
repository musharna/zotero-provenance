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
        empty_cache, "https://fixturehost.org/foo", "EXISTKEY", first_seen=date(2026, 5, 5)
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
        empty_cache, "https://fixturehost.org/foo", "EXISTKEY", first_seen=date(2026, 5, 1)
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
    """ZoteroError on POST → urls_new stays 0, error recorded, no cache insert."""
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
    assert lookup_url(empty_cache, "https://fixturehost.org/foo") is None


def test_capture_zotero_error_on_add_tags_records_error_no_last_seen_update(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """ZoteroError on add_tags → urls_recurring stays 0, error recorded, last_seen unchanged."""
    from zotero_capture.sqlite_cache import lookup_url
    from zotero_capture.zotero_client import ZoteroError

    insert_url(
        empty_cache, "https://fixturehost.org/foo", "EXISTKEY", first_seen=date(2026, 5, 1)
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
        empty_cache, "https://fixturehost.org/foo", "EXISTKEY", first_seen=date(2026, 5, 1)
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
