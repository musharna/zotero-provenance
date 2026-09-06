"""A downgrade stamps its own moment and keeps the host that answered.

`corroborate_github --apply` took ONE `now` before the loop and wrote it into
`last_attempt_at` for every row (the `hashed_at` shape: a per-run value where
a per-event one belongs), and wrote `final_url=""` -- which means "we never
found out where the request ended" -- over a host the original fetch HAD
recorded. Downgrading the outcome must not erase the address that produced it.
"""

from __future__ import annotations

import contextlib
import datetime
import sqlite3
from pathlib import Path

import pytest

import corroborate_github
from zotero_capture.snapshot import GONE, NOT_VISIBLE
from zotero_capture.sqlite_cache import init_db, insert_url, set_fetch_outcome


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "fake")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "0")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.setenv("ZOTERO_WEBSOURCES_COLLECTION_KEY", "FAKE0000")


def _seeded(tmp_path: Path) -> Path:
    db = tmp_path / "i.db"
    init_db(db)
    for i, (url, answered) in enumerate(
        [
            ("https://github.com/o/r1", "https://github.com/o/r1"),
            ("https://github.com/o/r2", "https://github.com/o/r2-renamed"),
        ]
    ):
        insert_url(db, url, f"K{i}", datetime.date(2026, 9, 1))
        set_fetch_outcome(db, url, outcome=GONE, at="T0", final_url=answered)
    return db


def _rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    with conn:
        return [dict(r) for r in conn.execute(
            "SELECT url_canonical, last_outcome, last_attempt_at, final_url FROM url_index ORDER BY 1"
        )]


def _run(db: Path, monkeypatch, clock):
    monkeypatch.setattr(corroborate_github, "repo_visibility", lambda slug, **kw: corroborate_github.VISIBLE)
    monkeypatch.setattr(corroborate_github, "read_token", lambda: "t")
    monkeypatch.setattr(corroborate_github, "build_fetch_client", lambda **kw: contextlib.nullcontext(object()))
    return corroborate_github.main(["--apply", "--db-path", str(db), "--sleep", "0"], clock=clock)


def test_each_downgrade_is_stamped_when_IT_happened(tmp_path, monkeypatch, creds) -> None:
    db = _seeded(tmp_path)
    ticks = iter(["T1", "T2", "T3"])
    reads: list[str] = []

    def clock() -> str:
        reads.append(next(ticks))
        return reads[-1]

    assert _run(db, monkeypatch, clock) == 0
    rows = _rows(db)
    assert [r["last_outcome"] for r in rows] == [NOT_VISIBLE, NOT_VISIBLE]
    stamps = [r["last_attempt_at"] for r in rows]
    assert len(set(stamps)) == 2, stamps
    assert len(reads) == 2, "the clock is read per ROW, not per run"


def test_a_downgrade_keeps_the_host_that_answered(tmp_path, monkeypatch, creds) -> None:
    db = _seeded(tmp_path)
    _run(db, monkeypatch, lambda: "T1")
    assert [r["final_url"] for r in _rows(db)] == [
        "https://github.com/o/r1",
        "https://github.com/o/r2-renamed",
    ]
