"""capture pipeline tests — orchestrates extract→exclude→dedup→POST/PATCH per spec §4."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zotero_capture.capture import CaptureResult, capture_message
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
        message="See https://example.com/foo for details.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    fake_zotero.post_webpage_item.assert_called_once()
    call = fake_zotero.post_webpage_item.call_args.kwargs
    assert call["url_canonical"] == "https://example.com/foo"
    assert call["access_date"] == "2026-05-05"
    assert "context:general" in call["tags"]
    assert "project:home" in call["tags"]
    assert "seen:2026-05-05" in call["tags"]
    assert "domain:example.com" in call["tags"]
    assert result.urls_new == 1


def test_capture_known_url_same_day_same_context_is_noop(
    empty_cache, fake_zotero, fake_title_fetcher
):
    insert_url(
        empty_cache, "https://example.com/foo", "EXISTKEY", first_seen=date(2026, 5, 5)
    )
    fake_zotero.add_tags.return_value = False
    result = capture_message(
        message="https://example.com/foo",
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
        empty_cache, "https://example.com/foo", "EXISTKEY", first_seen=date(2026, 5, 1)
    )
    result = capture_message(
        message="Source: https://example.com/foo",
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
        message="See http://localhost:3000/foo and https://example.com/bar",
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
        message="See https://example.com/ and https://example.com for details",
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
        message="See https://example.com/foo",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert result.urls_new == 0
    assert len(result.errors) == 1
    assert result.errors[0].url == "https://example.com/foo"
    assert result.errors[0].code == "zotero_error"
    assert lookup_url(empty_cache, "https://example.com/foo") is None


def test_capture_zotero_error_on_add_tags_records_error_no_last_seen_update(
    empty_cache, fake_zotero, fake_title_fetcher
):
    """ZoteroError on add_tags → urls_recurring stays 0, error recorded, last_seen unchanged."""
    from zotero_capture.sqlite_cache import lookup_url
    from zotero_capture.zotero_client import ZoteroError

    insert_url(
        empty_cache, "https://example.com/foo", "EXISTKEY", first_seen=date(2026, 5, 1)
    )
    fake_zotero.add_tags.side_effect = ZoteroError("simulated 503")
    result = capture_message(
        message="https://example.com/foo",
        project_slug="home",
        context="lit-review",
        today=date(2026, 5, 5),
        db_path=empty_cache,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert result.urls_recurring == 0
    assert len(result.errors) == 1
    row = lookup_url(empty_cache, "https://example.com/foo")
    assert row is not None
    assert row["last_seen"] == "2026-05-01"  # unchanged
