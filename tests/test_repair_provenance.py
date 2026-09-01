"""A repair must carry the provenance across, not leave it behind.

Repair moves a row from a broken URL to a corrected one, or merges it into an
existing row. Both are provenance operations, and both dropped provenance on the
floor: the queued sightings another session left under the old URL stayed there,
under a URL no index row will ever revisit again.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from zotero_capture.repair import RepairStep, apply_repair
from zotero_capture.sqlite_cache import init_db, peek_pending_tags, queue_pending_tags

BROKEN = "https://example.org/paper](https://example.org/paper"
FIXED = "https://example.org/paper"


def _connect(db: Path):
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _index(tmp_path: Path, rows) -> Path:
    db = tmp_path / "idx.db"
    init_db(db)
    with closing(_connect(db)) as conn:
        for url, key, first, last in rows:
            conn.execute(
                "INSERT INTO url_index"
                " (url_canonical, zotero_key, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?)",
                (url, key, first, last),
            )
    return db


class _Zotero:
    def __init__(self) -> None:
        self.tagged: list[tuple[str, list[str]]] = []
        self.trashed: list[str] = []

    def _patch_item_url(self, key, url, *, expect_url=None):
        return True

    def item_exists(self, key):
        return True

    def get_item_tags(self, key):
        return []

    def add_tags(self, key, tags, **kw):
        self.tagged.append((key, list(tags)))
        return True

    def trash_item(self, key, *, expect_url=None):
        self.trashed.append(key)
        return True


def test_a_rewrite_carries_the_queued_sightings_with_it(tmp_path: Path) -> None:
    """The queue is keyed by URL, and the rewrite moved the row out from under it."""
    db = _index(tmp_path, [(BROKEN, "K1", "2026-01-01", "2026-01-02")])
    queue_pending_tags(db, BROKEN, ["project:x", "context:user-shared"])

    apply_repair(
        [RepairStep(url=BROKEN, corrected=FIXED, zotero_key="K1", action="rewrite")],
        db_path=db, zotero=_Zotero(), connect=_connect,
    )

    assert peek_pending_tags(db, BROKEN) == [], "the sighting was stranded"
    assert sorted(peek_pending_tags(db, FIXED)) == [
        "context:user-shared", "project:x",
    ]


def test_a_merge_applies_the_queued_sightings_to_the_survivor(tmp_path: Path) -> None:
    """The duplicate's row is deleted, so its queue has nowhere left to go."""
    db = _index(
        tmp_path,
        [(BROKEN, "K1", "2026-01-01", "2026-01-02"),
         (FIXED, "K2", "2026-02-01", "2026-02-02")],
    )
    queue_pending_tags(db, BROKEN, ["project:x"])

    zotero = _Zotero()
    apply_repair(
        [RepairStep(url=BROKEN, corrected=FIXED, zotero_key="K1", action="merge")],
        db_path=db, zotero=zotero, connect=_connect,
    )

    assert zotero.trashed == ["K1"], "positive control: the duplicate was merged"
    applied = [t for key, tags in zotero.tagged if key == "K2" for t in tags]
    assert "project:x" in applied, f"the sighting was lost: {zotero.tagged}"
    assert peek_pending_tags(db, BROKEN) == []


def test_a_merge_keeps_the_earlier_first_seen(tmp_path: Path) -> None:
    """Two rows for one source: the source was first seen on the earlier date."""
    db = _index(
        tmp_path,
        [(BROKEN, "K1", "2026-01-01", "2026-01-02"),
         (FIXED, "K2", "2026-02-01", "2026-02-02")],
    )

    apply_repair(
        [RepairStep(url=BROKEN, corrected=FIXED, zotero_key="K1", action="merge")],
        db_path=db, zotero=_Zotero(), connect=_connect,
    )

    with closing(_connect(db)) as conn:
        row = conn.execute(
            "SELECT first_seen, last_seen FROM url_index WHERE url_canonical = ?",
            (FIXED,),
        ).fetchone()
    assert row["first_seen"] == "2026-01-01"
    assert row["last_seen"] == "2026-02-02"
