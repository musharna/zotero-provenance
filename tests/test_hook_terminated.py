"""A hook timeout is a fact about US, and it must leave a structured record.

`HookTerminated` derives from BaseException so the per-URL loop cannot swallow
it, and `main()` caught only `Exception`, so the timeout escaped as a raw
traceback into capture.log. Health then read those lines as "unreadable log"
and the next successful capture erased the finding. The live capture.log held
two of them when this was written (2026-09-06).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import zotero_capture.cli as cli
from zotero_capture.cli import main
from zotero_capture.health import REFUSAL_EVENTS


def _events(tmp_path: Path) -> list[dict]:
    try:
        text = (tmp_path / "capture.log").read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_WEBSOURCES_COLLECTION_KEY", "C")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.delenv("ZOTERO_CAPTURE_DISABLE", raising=False)

    def _cut_off(*a, **k):
        raise cli.HookTerminated("terminated by signal 15")

    monkeypatch.setattr(cli, "build_client", _cut_off)
    return tmp_path


def test_a_timeout_writes_a_hook_terminated_event(configured) -> None:
    rc = main(["--cwd", "/tmp", "--session", "s", "--message", "see https://x.test/a"])

    assert rc == 0, "a hook timeout must not block a turn"
    events = [e for e in _events(configured) if e.get("event") == "hook-terminated"]
    assert events, _events(configured)
    assert "signal 15" in str(events[0].get("detail")), events[0]
    # Positive control: an event health does not count is written and never
    # reported, which is the defect one layer up.
    assert "hook-terminated" in REFUSAL_EVENTS


def test_the_traceback_no_longer_reaches_the_log(configured, capsys) -> None:
    main(["--cwd", "/tmp", "--message", "see https://x.test/a"])
    assert "Traceback" not in capsys.readouterr().err


# --- a POST the timeout cut off must be revisited -----------------------------


def test_a_post_cut_off_by_the_timeout_is_queued(tmp_path) -> None:
    """The claim must STAND (the POST may have committed) AND the URL must be
    queued, so a drain settles it through the reservation protocol. Before
    0.58.0 the claim stood and nothing ever revisited it -- _resolve_claim
    runs only when the URL is cited AGAIN, and two live rows had sat for five
    and six days with no item recorded."""
    import sqlite3
    from datetime import date
    from unittest.mock import MagicMock

    from zotero_capture.capture import capture_message
    from zotero_capture.sqlite_cache import init_db, retry_queue_depth

    db = tmp_path / "i.db"
    init_db(db)
    z = MagicMock()
    z.post_webpage_item.side_effect = cli.HookTerminated("terminated by signal 15")

    with pytest.raises(cli.HookTerminated):
        capture_message(
            message="see https://fixturehost.org/cut",
            project_slug="home",
            context=None,
            today=date(2026, 9, 6),
            db_path=db,
            zotero=z,
            title_fetcher=lambda u: "T",
        )

    assert retry_queue_depth(db) == 1
    # Positive control: the claim was NOT released -- the POST went out.
    row = sqlite3.connect(db).execute(
        "SELECT zotero_key, pending_key FROM url_index"
    ).fetchone()
    assert row is not None and row[0] == "" and row[1], row


# --- and a drain must SETTLE it, not drop it ----------------------------------


def _cut_off_capture(db, url="https://fixturehost.org/cut"):
    from datetime import date
    from unittest.mock import MagicMock

    from zotero_capture.capture import capture_message

    z = MagicMock()
    z.post_webpage_item.side_effect = cli.HookTerminated("terminated by signal 15")
    with pytest.raises(cli.HookTerminated):
        capture_message(
            message=f"see {url}",
            project_slug="home",
            context=None,
            today=date(2026, 9, 6),
            db_path=db,
            zotero=z,
            title_fetcher=lambda u: "T",
        )
    return url


def _replay_with(db, zotero, *, now):
    from datetime import date

    from zotero_capture.capture import capture_message

    def replay(entry):
        return capture_message(
            message=entry["url_canonical"],
            project_slug=entry["project"],
            context=entry["context"],
            today=date.fromisoformat(entry["seen_date"]),
            db_path=db,
            zotero=zotero,
            title_fetcher=lambda u: "T",
            now=now,
        )

    return replay


def test_a_drain_settles_an_old_claim_by_asking_zotero(tmp_path) -> None:
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    from zotero_capture.drain import drain
    from zotero_capture.sqlite_cache import init_db, retry_queue_depth

    db = tmp_path / "i.db"
    init_db(db)
    url = _cut_off_capture(db)
    pending = sqlite3.connect(db).execute("SELECT pending_key FROM url_index").fetchone()[0]

    z = MagicMock()
    z.item_exists.return_value = True  # the POST had committed
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    drain(db, replay=_replay_with(db, z, now=later))

    row = sqlite3.connect(db).execute("SELECT zotero_key FROM url_index").fetchone()
    assert row[0] == pending, row
    assert retry_queue_depth(db) == 0
    z.post_webpage_item.assert_not_called()  # positive control: no duplicate


def test_a_drain_inside_the_claim_window_keeps_the_entry_queued(tmp_path) -> None:
    """Within STALE_CLAIM_S the claim is left alone by design. A drain that
    ran then saw a replay with no errors and called it recovered -- dropping
    the only record that anything was owed."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from zotero_capture.drain import drain
    from zotero_capture.sqlite_cache import init_db, retry_queue_depth

    db = tmp_path / "i.db"
    init_db(db)
    _cut_off_capture(db)

    z = MagicMock()
    drain(db, replay=_replay_with(db, z, now=datetime.now(timezone.utc)))

    assert retry_queue_depth(db) == 1
    z.item_exists.assert_not_called()  # positive control: the window was honoured
