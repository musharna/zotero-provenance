"""sqlite_cache module tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    lookup_url,
    update_last_seen,
)


def test_init_db_creates_schema(tmp_db: Path):
    init_db(tmp_db)
    assert tmp_db.exists()
    # Re-init is idempotent
    init_db(tmp_db)


def test_lookup_url_returns_none_for_missing(tmp_db: Path):
    init_db(tmp_db)
    assert lookup_url(tmp_db, "https://fixturehost.org/foo") is None


def test_insert_then_lookup_roundtrip(tmp_db: Path):
    init_db(tmp_db)
    insert_url(
        tmp_db,
        url_canonical="https://fixturehost.org/foo",
        zotero_key="ABC123",
        first_seen=date(2026, 5, 5),
    )
    row = lookup_url(tmp_db, "https://fixturehost.org/foo")
    assert row is not None
    assert row["zotero_key"] == "ABC123"
    assert row["first_seen"] == "2026-05-05"
    assert row["last_seen"] == "2026-05-05"


def test_update_last_seen(tmp_db: Path):
    init_db(tmp_db)
    insert_url(tmp_db, "https://fixturehost.org/foo", "ABC123", date(2026, 5, 1))
    update_last_seen(tmp_db, "https://fixturehost.org/foo", date(2026, 5, 5))
    row = lookup_url(tmp_db, "https://fixturehost.org/foo")
    assert row is not None
    assert row["last_seen"] == "2026-05-05"
    assert row["first_seen"] == "2026-05-01"


def test_insert_url_duplicate_silently_ignored(tmp_db: Path):
    init_db(tmp_db)
    insert_url(tmp_db, "https://fixturehost.org/dup", "KEY_A", date(2026, 5, 1))
    # Second insert with a different key must not raise
    insert_url(tmp_db, "https://fixturehost.org/dup", "KEY_B", date(2026, 5, 5))
    row = lookup_url(tmp_db, "https://fixturehost.org/dup")
    assert row is not None
    # First writer wins — key and dates must reflect the first insert
    assert row["zotero_key"] == "KEY_A"
    assert row["first_seen"] == "2026-05-01"
    assert row["last_seen"] == "2026-05-01"


def test_update_last_seen_raises_keyerror_when_missing(tmp_db: Path):
    init_db(tmp_db)
    url = "https://fixturehost.org/not-there"
    with pytest.raises(KeyError, match=repr(url)):
        update_last_seen(tmp_db, url, date(2026, 5, 5))
