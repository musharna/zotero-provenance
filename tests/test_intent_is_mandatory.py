"""If the incident cannot be recorded, the write must not happen.

0.20.0 journals the intent before each mutation, which is the whole point. But
`_record_intent` swallowed any failure and let the write proceed — so a
read-only state directory, a full disk, or a permission error reproduced the
exact condition the release exists to prevent: a row in the library with no
record that anything wrote it.

Refusing is consistent with the rest of this plugin, which declines to write
whenever it cannot establish that writing is safe. A skipped citation is
recoverable; an unrecorded mutation is not.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import zotero_capture.capture as cap
from zotero_capture.capture import capture_message


class _Recorder:
    def __init__(self) -> None:
        self.posted: list[str] = []

    def post_webpage_item(self, *, url_canonical, **kw):
        self.posted.append(url_canonical)
        return "KEY1"

    def add_tags(self, *a, **k):
        return None


def _run(tmp_path: Path, ledger, monkeypatch, *, root="/c/OLD", pinned="/c/NEW"):
    zotero = _Recorder()
    capture_message(
        message="see https://fixturehost.org/one",
        project_slug="p",
        context=None,
        today=date(2026, 8, 25),
        db_path=tmp_path / "i.db",
        zotero=zotero,
        title_fetcher=lambda url: "t",
        incident_id="inc-1",
        pinned_root=pinned,
        running_root=root,
        ledger_path=ledger,
    )
    return zotero


def test_a_write_is_refused_when_its_incident_cannot_be_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(cap, "open_incident", _boom)

    zotero = _run(tmp_path, tmp_path / "health.db", monkeypatch)

    assert zotero.posted == [], "wrote to Zotero with no incident recorded"


def test_the_same_write_succeeds_when_the_incident_can_be_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    """Positive control, so the refusal above cannot pass by breaking capture."""
    zotero = _run(tmp_path, tmp_path / "health.db", monkeypatch)

    assert zotero.posted == ["https://fixturehost.org/one"]


def test_a_healthy_write_is_unaffected_by_a_broken_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    """A healthy capture opens no incident, so it must not depend on the ledger."""
    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(cap, "open_incident", _boom)

    zotero = _run(tmp_path, tmp_path / "health.db", monkeypatch,
                  root="/c/SAME", pinned="/c/SAME")

    assert zotero.posted == ["https://fixturehost.org/one"]
