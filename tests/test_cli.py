"""CLI tests — argparse + --triage flow."""

from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock


from zotero_capture.cli import build_parser, run_capture, run_triage
from zotero_capture.sqlite_cache import init_db, insert_url


def test_parser_capture_mode():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--project",
            "home",
            "--session",
            "abc-123",
            "--context",
            "lit-review",
            "--message-from-stdin",
        ]
    )
    assert args.project == "home"
    assert args.context == "lit-review"
    assert args.message_from_stdin
    assert args.triage is None


def test_parser_triage_mode():
    parser = build_parser()
    args = parser.parse_args(["--triage", "https://fixturehost.org/foo"])
    assert args.triage == "https://fixturehost.org/foo"


def test_parser_derives_project_from_cwd_when_not_given():
    args = build_parser().parse_args(["--cwd", "/home/someone/agrigen"])
    assert args.project is None
    assert args.cwd == "/home/someone/agrigen"


def test_run_triage_when_url_unknown_returns_two(tmp_db: Path):
    init_db(tmp_db)
    fake_zotero = MagicMock()
    rc = run_triage(
        url="https://fixturehost.org/missing",
        db_path=tmp_db,
        zotero=fake_zotero,
    )
    assert rc == 2
    fake_zotero.add_tags.assert_not_called()


def test_run_triage_adds_tag_when_url_known(tmp_db: Path):
    init_db(tmp_db)
    insert_url(
        tmp_db, "https://fixturehost.org/foo", "EXISTKEY", first_seen=date(2026, 5, 1)
    )
    fake_zotero = MagicMock()
    fake_zotero.add_tags.return_value = True
    rc = run_triage(
        url="https://fixturehost.org/foo",
        db_path=tmp_db,
        zotero=fake_zotero,
    )
    assert rc == 0
    fake_zotero.add_tags.assert_called_once_with("EXISTKEY", ["triaged"])


def test_run_triage_matches_a_non_canonical_url(tmp_db: Path):
    """The user pastes the URL they saw, which may carry tracking params."""
    init_db(tmp_db)
    insert_url(
        tmp_db, "https://fixturehost.org/foo", "EXISTKEY", first_seen=date(2026, 5, 1)
    )
    fake_zotero = MagicMock()
    rc = run_triage(
        url="https://Fixturehost.org/foo/?utm_source=newsletter#section",
        db_path=tmp_db,
        zotero=fake_zotero,
    )
    assert rc == 0
    fake_zotero.add_tags.assert_called_once_with("EXISTKEY", ["triaged"])


def test_run_capture_reads_message_from_stdin(tmp_db: Path, monkeypatch):
    init_db(tmp_db)
    fake_zotero = MagicMock()
    fake_zotero.post_webpage_item.return_value = "NEWKEY"
    monkeypatch.setattr("sys.stdin", io.StringIO("see https://fixturehost.org/foo"))
    result = run_capture(
        message=None,
        project="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=tmp_db,
        zotero=fake_zotero,
        title_fetcher=lambda u, **kw: "T",
        log_path=tmp_db.parent / "log.jsonl",
        retry_queue_path=tmp_db.parent / "retry.jsonl",
    )
    assert result.urls_new == 1


def test_run_capture_does_not_enqueue_failures(tmp_db: Path):
    """Phase-1: failures surface in result.errors + the JSON log, never the retry queue.

    The retry handler is a stub returning False; queueing would accumulate
    forever. Revisit when phase-2 ships real retry semantics (spec §7).
    """
    from zotero_capture.zotero_client import ZoteroError

    init_db(tmp_db)
    fake_zotero = MagicMock()
    call_count = [0]

    def side_effect(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ZoteroError("simulated 500")
        raise RuntimeError("unexpected bug")

    fake_zotero.post_webpage_item.side_effect = side_effect

    retry_queue_path = tmp_db.parent / "retry.jsonl"
    log_path = tmp_db.parent / "log.jsonl"

    result = run_capture(
        message="see https://fixturehost.org/foo and https://fixturehost.org/bar",
        project="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=tmp_db,
        zotero=fake_zotero,
        title_fetcher=lambda u, **kw: "T",
        log_path=log_path,
        retry_queue_path=retry_queue_path,
    )

    assert not retry_queue_path.exists(), "phase-1: nothing should be enqueued"
    error_codes = {e.code for e in result.errors}
    assert error_codes == {"zotero_error", "unexpected"}
    assert log_path.exists(), "errors must still be logged"
    log_obj = json.loads(log_path.read_text().strip())
    logged_codes = {e["code"] for e in log_obj["errors"]}
    assert logged_codes == {"zotero_error", "unexpected"}


def test_build_client_accepts_a_longer_timeout_for_unattended_passes():
    """The hook wants a tight timeout; a bulk maintenance sweep does not.

    Paging thousands of items with the interactive 5s budget made whole passes
    abort on a slow response (observed live 2026-08-21).
    """
    from zotero_capture.cli import build_client
    from zotero_capture.config import Config

    cfg = Config(
        api_key="k",
        library_id="1",
        library_type="user",
        collection_key="C",
        state_dir=Path("/tmp"),
    )
    assert build_client(cfg, timeout=30.0)._client.timeout.read == 30.0
    assert build_client(cfg)._client.timeout.read == 5.0
