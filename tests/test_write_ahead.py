"""The incident must exist before the Zotero call that could corrupt a row.

The test that matters here is the one where the process dies mid-write. If the
incident is only recorded afterwards, a hook timeout leaves a row in the library
from a superseded root with nothing anywhere saying so — and the health check
cannot report what was never written down.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import zotero_capture.capture as cap
from zotero_capture.capture import capture_message
from zotero_capture.health_ledger import count_open, open_incidents


class _DiesMidWrite:
    """A client that commits remotely and then fails, like a timeout would."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    def post_webpage_item(self, *, url_canonical, title, tags, item_key=None, **kw):
        self.posted.append(url_canonical)
        raise TimeoutError("killed after the row was created")

    def add_tags(self, *a, **k):  # pragma: no cover - not reached here
        raise AssertionError("unexpected")


def _run(db: Path, ledger: Path, monkeypatch, *, pinned: str | None, root: str):
    monkeypatch.setattr(cap, "_running_root", lambda: root, raising=False)
    zotero = _DiesMidWrite()
    # capture_message swallows the failure by design — a capture must never
    # block a turn — so the row is written, the error is recorded, and the
    # process could equally have been killed here by the hook timeout.
    capture_message(
        message="see https://fixturehost.org/one",
        project_slug="p",
        context=None,
        today=date(2026, 8, 25),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda url: "t",
        incident_id="inc-1",
        pinned_root=pinned,
        running_root=root,
        ledger_path=ledger,
    )
    return zotero


def test_a_write_from_a_stale_root_is_recorded_before_it_happens(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = tmp_path / "health.db"
    zotero = _run(tmp_path / "i.db", ledger, monkeypatch,
                  pinned="/c/NEW", root="/c/OLD")

    assert zotero.posted, "positive control: the write should have been attempted"
    found = open_incidents(ledger)
    assert found, "the row was written with no incident recorded"
    assert found[0]["kind"] == "stale"
    assert found[0]["url"] == "https://fixturehost.org/one"


def test_a_write_with_an_unverifiable_pin_is_recorded_too(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = tmp_path / "health.db"
    _run(tmp_path / "i.db", ledger, monkeypatch, pinned=None, root="/c/OLD")

    found = open_incidents(ledger)
    assert found and found[0]["kind"] == "unverified", found


def test_a_healthy_write_records_no_incident(tmp_path: Path, monkeypatch) -> None:
    """A correct install must not accumulate ledger rows for ordinary work."""
    ledger = tmp_path / "health.db"
    _run(tmp_path / "i.db", ledger, monkeypatch, pinned="/c/SAME", root="/c/SAME")

    assert count_open(ledger) == 0
