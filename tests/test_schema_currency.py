"""An index older than the code that opens it must still be readable.

Found by real execution, never by the suite: `snapshot_pages.py --limit 5` died
on the live index with `no such column: last_outcome`. Every test calls
`init_db` in its fixture, so migrations always ran and the gap was invisible.

`init_db` is the only thing that applies MIGRATIONS, and it was called by 2 of
the 8 maintenance CLIs. So a column added for a maintenance tool was missing in
exactly the tool that needed it. Writing the call into the other six would be
the same rule in eight places, and the ninth tool would omit it -- this
codebase's three worst defects have all been a second copy of one rule going
stale, against zero caused by a missing guard.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from zotero_capture.sqlite_cache import (
    insert_url,
    row_for_url,
    rows_needing_hash,
)

# url_index as it shipped, before any migration added a column to it.
ORIGINAL_SCHEMA = """
CREATE TABLE url_index (
    url_canonical TEXT PRIMARY KEY,
    zotero_key    TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
"""


def _legacy_index(tmp_path: Path) -> Path:
    """A database from before the current schema, opened by nothing since."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(ORIGINAL_SCHEMA)
    conn.execute(
        "INSERT INTO url_index VALUES ('https://fixturehost.org/a', 'KEY1',"
        " '2026-05-05', '2026-05-05')"
    )
    conn.commit()
    conn.close()
    return db


def test_a_reader_opens_an_index_older_than_its_own_schema(tmp_path: Path) -> None:
    """No init_db call -- exactly what six of the eight maintenance CLIs do."""
    db = _legacy_index(tmp_path)

    rows = rows_needing_hash(db)

    assert [r["url_canonical"] for r in rows] == ["https://fixturehost.org/a"], (
        "the pre-existing row must survive the migration, not just the query"
    )


def test_the_migrated_columns_are_actually_there(tmp_path: Path) -> None:
    """Positive control. A reader that silently returned [] would satisfy the
    test above while still having no schema at all."""
    db = _legacy_index(tmp_path)

    row = row_for_url(db, "https://fixturehost.org/a")

    assert row is not None
    for column in ("content_hash", "hashed_at", "last_outcome", "last_attempt_at"):
        assert column in row, f"{column} never reached the legacy index"
    assert row["last_outcome"] == "", "an untouched legacy row must read as untried"


def test_a_writer_reaches_a_legacy_index_too(tmp_path: Path) -> None:
    db = _legacy_index(tmp_path)

    insert_url(db, "https://fixturehost.org/b", "KEY2", date(2026, 5, 6))

    assert row_for_url(db, "https://fixturehost.org/b") is not None
