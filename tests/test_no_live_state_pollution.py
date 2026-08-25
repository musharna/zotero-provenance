"""A capture given explicit paths must not reach into the environment.

0.20.1 fixed a real divergence — the capture path derived the ledger from
`log_path.parent` while the checker derived it from the state directory — by
having the capture path read `_state_dir(os.environ)`. That fix introduced a
worse problem: `run_capture` was now consulting the environment for a sibling of
paths it had already been handed, so the test suite wrote two real integrity
incidents into the developer's live ledger.

The rule this encodes: a function given explicit paths derives everything from
them. Only `main()`, which owns the environment, resolves the state directory —
and it passes both paths down together, so writer and reader still agree.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from zotero_capture.cli import run_capture
from zotero_capture.sqlite_cache import init_db


def test_run_capture_writes_no_ledger_outside_the_paths_it_was_given(
    tmp_path: Path, monkeypatch
) -> None:
    live = tmp_path / "pretend-live-state"
    live.mkdir()
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(live))

    db = tmp_path / "i.db"
    init_db(db)
    zotero = MagicMock()
    zotero.post_webpage_item.return_value = "NEWKEY"
    monkeypatch.setattr("sys.stdin", io.StringIO("see https://fixturehost.org/foo"))

    run_capture(
        message=None,
        project="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda u, **kw: "T",
        log_path=tmp_path / "log.jsonl",
        ledger_path=tmp_path / "health.db",
    )

    assert not (live / "health.db").exists(), (
        "run_capture wrote a ledger into the environment's state directory "
        "instead of the path it was given"
    )
    assert (tmp_path / "log.jsonl").exists(), "positive control: it did capture"
