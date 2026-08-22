"""SQLite-backed URL cache for O(1) dedup against Zotero `web-sources`."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import TypedDict, cast

SCHEMA = """
CREATE TABLE IF NOT EXISTS url_index (
    url_canonical TEXT PRIMARY KEY,
    zotero_key    TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
"""


class URLCacheRow(TypedDict):
    url_canonical: str
    zotero_key: str
    first_seen: str
    last_seen: str


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Multiple Claude sessions capture concurrently into one index; wait on the
    # write lock instead of raising "database is locked" immediately.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: Path) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(SCHEMA)


def lookup_url(db_path: Path, url_canonical: str) -> URLCacheRow | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT url_canonical, zotero_key, first_seen, last_seen FROM url_index WHERE url_canonical = ?",
            (url_canonical,),
        ).fetchone()
    return cast(URLCacheRow, dict(row)) if row else None


def insert_url(
    db_path: Path, url_canonical: str, zotero_key: str, first_seen: date
) -> None:
    iso = first_seen.isoformat()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO url_index (url_canonical, zotero_key, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            (url_canonical, zotero_key, iso, iso),
        )


def reserve_url(db_path: Path, url_canonical: str, first_seen: date) -> bool:
    """Claim a URL before creating its Zotero item. True if this caller won.

    `lookup -> POST -> insert` is not atomic: two sessions could both miss the
    lookup, both POST, and then `INSERT OR IGNORE` would keep one key and drop
    the other — leaving a real Zotero item with nothing in the index pointing at
    it, invisible to dedup forever. The detached prompt hook makes overlapping
    captures ordinary, so this is a race that actually runs.

    The row is written first with an empty key, which the PRIMARY KEY makes
    atomic across processes. `set_zotero_key` fills it in once the item exists;
    `release_url` undoes the claim if the POST fails.
    """
    iso = first_seen.isoformat()
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO url_index (url_canonical, zotero_key, first_seen, last_seen) VALUES (?, '', ?, ?)",
            (url_canonical, iso, iso),
        )
    return cursor.rowcount == 1


def set_zotero_key(db_path: Path, url_canonical: str, zotero_key: str) -> None:
    """Complete a reservation once the Zotero item exists."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE url_index SET zotero_key = ? WHERE url_canonical = ?",
            (zotero_key, url_canonical),
        )


def release_url(db_path: Path, url_canonical: str) -> None:
    """Drop an unfulfilled reservation so a later run can retry.

    Only removes a row that never got a key. A completed row belongs to a real
    Zotero item, and deleting its index entry would strand that item exactly the
    way the un-reserved race did.
    """
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "DELETE FROM url_index WHERE url_canonical = ? AND zotero_key = ''",
            (url_canonical,),
        )


def update_last_seen(db_path: Path, url_canonical: str, seen: date) -> None:
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE url_index SET last_seen = ? WHERE url_canonical = ?",
            (seen.isoformat(), url_canonical),
        )
    if cursor.rowcount == 0:
        raise KeyError(f"url_canonical not found in cache: {url_canonical!r}")
