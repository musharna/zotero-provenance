"""sqlite_cache module tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    lookup_url,
    release_url,
    reserve_url,
    set_zotero_key,
    update_last_seen,
)


def _reserve_worker(args: tuple[str]) -> bool:
    """Top level so multiprocessing can pickle it."""
    (path,) = args
    return reserve_url(Path(path), "https://fixturehost.org/race", date(2026, 5, 5))


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


# --- reservation (F5) ---
#
# lookup -> POST -> INSERT OR IGNORE is not atomic. Two sessions could both miss
# the lookup, both create a Zotero item, and then one key would be silently
# dropped by the IGNORE, orphaning the other item forever. The detached prompt
# hook makes overlapping captures ordinary, not hypothetical.


def test_only_one_reservation_wins(tmp_db):
    init_db(tmp_db)
    first = reserve_url(tmp_db, "https://fixturehost.org/a", date(2026, 5, 5))
    second = reserve_url(tmp_db, "https://fixturehost.org/a", date(2026, 5, 5))

    assert first is True
    assert second is False, "a second session must not also claim the URL"


def test_a_reservation_starts_with_no_key_and_is_filled_in(tmp_db):
    init_db(tmp_db)
    reserve_url(tmp_db, "https://fixturehost.org/a", date(2026, 5, 5))
    assert lookup_url(tmp_db, "https://fixturehost.org/a")["zotero_key"] == ""

    set_zotero_key(tmp_db, "https://fixturehost.org/a", "KEY1")
    assert lookup_url(tmp_db, "https://fixturehost.org/a")["zotero_key"] == "KEY1"


def test_releasing_a_reservation_lets_a_later_run_retry(tmp_db):
    """A POST that fails must not leave the URL permanently claimed."""
    init_db(tmp_db)
    reserve_url(tmp_db, "https://fixturehost.org/a", date(2026, 5, 5))
    release_url(tmp_db, "https://fixturehost.org/a")

    assert lookup_url(tmp_db, "https://fixturehost.org/a") is None
    assert reserve_url(tmp_db, "https://fixturehost.org/a", date(2026, 5, 6)) is True


def test_release_never_discards_a_completed_row(tmp_db):
    """Releasing after the key landed would delete a real item's index entry."""
    init_db(tmp_db)
    reserve_url(tmp_db, "https://fixturehost.org/a", date(2026, 5, 5))
    set_zotero_key(tmp_db, "https://fixturehost.org/a", "KEY1")
    release_url(tmp_db, "https://fixturehost.org/a")

    assert lookup_url(tmp_db, "https://fixturehost.org/a")["zotero_key"] == "KEY1"


def test_concurrent_processes_produce_exactly_one_winner(tmp_db):
    """Real execution, not a simulated race: separate processes, one shared file."""
    import multiprocessing

    init_db(tmp_db)
    with multiprocessing.Pool(8) as pool:
        results = pool.map(_reserve_worker, [(str(tmp_db),)] * 8)

    assert sum(1 for r in results if r) == 1, f"exactly one winner expected, got {results}"
