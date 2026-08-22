"""SQLite-backed URL cache for O(1) dedup against Zotero `web-sources`."""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

SCHEMA = """
CREATE TABLE IF NOT EXISTS url_index (
    url_canonical TEXT PRIMARY KEY,
    zotero_key    TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_tags (
    url_canonical TEXT NOT NULL,
    tag           TEXT NOT NULL,
    PRIMARY KEY (url_canonical, tag)
);
"""

# Added after the original table shipped, so they arrive by migration rather than
# in SCHEMA: the deployed index already holds thousands of rows.
MIGRATIONS = (
    "ALTER TABLE url_index ADD COLUMN pending_key TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE url_index ADD COLUMN claimed_at TEXT NOT NULL DEFAULT ''",
)

# The alphabet the Zotero API accepts for an object key: base32 without the
# characters that read ambiguously (0/O, 1/I). Keys are 8 of these.
ZOTERO_KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
ZOTERO_KEY_RE = re.compile(rf"[{ZOTERO_KEY_ALPHABET}]{{8}}")


def new_zotero_key() -> str:
    """A key this client picks itself, so a lost response is still answerable.

    Choosing the key before the POST is what turns "did my item get created?"
    from unanswerable into a single GET. secrets rather than random because the
    key must not collide with another session's concurrent choice.
    """
    return "".join(secrets.choice(ZOTERO_KEY_ALPHABET) for _ in range(8))


class URLCacheRow(TypedDict):
    url_canonical: str
    zotero_key: str
    first_seen: str
    last_seen: str
    pending_key: str
    claimed_at: str


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
        conn.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as e:
                # Already applied. Anything else is a real problem and re-raises.
                if "duplicate column name" not in str(e):
                    raise


def lookup_url(db_path: Path, url_canonical: str) -> URLCacheRow | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT url_canonical, zotero_key, first_seen, last_seen, pending_key,"
            " claimed_at FROM url_index WHERE url_canonical = ?",
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


def reserve_url(
    db_path: Path,
    url_canonical: str,
    first_seen: date,
    *,
    pending_key: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Claim a URL before creating its Zotero item. True if this caller won.

    `lookup -> POST -> insert` is not atomic: two sessions could both miss the
    lookup, both POST, and then `INSERT OR IGNORE` would keep one key and drop
    the other — leaving a real Zotero item with nothing in the index pointing at
    it, invisible to dedup forever. The detached prompt hook makes overlapping
    captures ordinary, so this is a race that actually runs.

    The row is written first with an empty key, which the PRIMARY KEY makes
    atomic across processes. `set_zotero_key` fills it in once the item exists;
    `release_url` undoes the claim if the POST was never issued.

    The claim also records the key the caller intends to create and the moment it
    was taken. Without those, an abandoned claim is ambiguous — it may or may not
    already have an item behind it, so neither completing it nor releasing it is
    safe. With them, the question is one GET.
    """
    iso = first_seen.isoformat()
    key = pending_key if pending_key is not None else new_zotero_key()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO url_index"
            " (url_canonical, zotero_key, first_seen, last_seen, pending_key, claimed_at)"
            " VALUES (?, '', ?, ?, ?, ?)",
            (url_canonical, iso, iso, key, stamp),
        )
    return cursor.rowcount == 1


def queue_pending_tags(db_path: Path, url_canonical: str, tags: list[str]) -> None:
    """Remember tags that could not be applied yet, so the sighting is not lost.

    A session that loses the race has real provenance to record — its own
    context and project — but no item to put it on, because the winner's POST is
    still in flight. Dropping it silently lost that occurrence for good if the
    URL was never cited again.
    """
    with closing(_connect(db_path)) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO pending_tags (url_canonical, tag) VALUES (?, ?)",
            [(url_canonical, tag) for tag in tags],
        )


def take_pending_tags(db_path: Path, url_canonical: str) -> list[str]:
    """Remove and return the tags queued for a URL. Empty if there were none."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT tag FROM pending_tags WHERE url_canonical = ? ORDER BY tag",
            (url_canonical,),
        ).fetchall()
        conn.execute(
            "DELETE FROM pending_tags WHERE url_canonical = ?", (url_canonical,)
        )
    return [r["tag"] for r in rows]


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
