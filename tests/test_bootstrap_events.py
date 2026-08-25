"""Failures BEFORE a capture starts must reach the log as structured events.

A missing credential wrote one plaintext line to stderr, which the hook appended
to the capture log, where the health parser dropped it for not being JSON. So a
fresh install with no API key could fail on every cited URL, forever, and the
check that exists to notice exactly that stayed silent.

The same held for any unexpected exception outside run_capture(): a traceback is
not a record. Both now emit a timestamped event, written through _state_dir,
which resolves without valid credentials — that is the whole point, since these
are the paths where credentials are what is missing.
"""

from __future__ import annotations

import json
from pathlib import Path

from zotero_capture.cli import main


def _log(tmp_path: Path) -> Path:
    return tmp_path / "capture.log"


def _events(tmp_path: Path) -> list[dict]:
    try:
        text = _log(tmp_path).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def test_a_missing_credential_writes_a_structured_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(tmp_path))
    for var in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_WEBSOURCES_COLLECTION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ZOTERO_SECRETS_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("ZOTERO_CAPTURE_DISABLE", raising=False)

    rc = main(["--cwd", "/tmp", "--session", "s", "--message", "see https://x.test/a"])

    assert rc == 0, "a misconfigured plugin must not block a turn"
    events = [e for e in _events(tmp_path) if e.get("event") == "configuration-error"]
    assert events, _events(tmp_path)
    assert events[0].get("ts"), events[0]
    assert "ZOTERO_API_KEY" in str(events[0].get("detail")), events[0]


def test_the_event_is_visible_to_the_health_check(tmp_path, monkeypatch) -> None:
    """The whole point of making it structured."""
    from zotero_capture.health import evaluate

    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(tmp_path))
    for var in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_WEBSOURCES_COLLECTION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ZOTERO_SECRETS_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("ZOTERO_CAPTURE_DISABLE", raising=False)

    main(["--cwd", "/tmp", "--session", "s", "--message", "see https://x.test/a"])

    warnings = evaluate(_log(tmp_path).read_text().splitlines(), pinned_root=None)

    assert warnings, "the health check still could not see a broken install"
    assert "configuration-error" in warnings[0], warnings


def test_an_unexpected_failure_writes_a_bootstrap_event(tmp_path, monkeypatch) -> None:
    import zotero_capture.cli as cli

    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_WEBSOURCES_COLLECTION_KEY", "C")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.delenv("ZOTERO_CAPTURE_DISABLE", raising=False)

    def _boom(*a, **k):
        raise RuntimeError("deliberate")

    monkeypatch.setattr(cli, "build_client", _boom)

    rc = main(["--cwd", "/tmp", "--session", "s", "--message", "see https://x.test/a"])

    assert rc == 0
    events = [e for e in _events(tmp_path) if e.get("event") == "capture-bootstrap-error"]
    assert events, _events(tmp_path)
    assert "deliberate" in str(events[0].get("detail")), events[0]
