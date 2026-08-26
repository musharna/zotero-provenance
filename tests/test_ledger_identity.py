"""One ledger row per mutation, not one per capture.

The incident id was minted once in `run_capture` and reused for every URL in
the message. `incident_id` is the ledger's PRIMARY KEY and the insert says ON
CONFLICT DO NOTHING, so a message citing three sources from a superseded root
recorded ONE incident: it named the first URL, silently discarded the other
two, and handed a person an `--ack` that closed evidence they were never shown.

That is the exact failure the write-ahead ledger exists to prevent, one layer
inside the fix for it. The id must name what the row names — a single write of
a single URL.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from zotero_capture.capture import _record_intent, capture_message
from zotero_capture.health_ledger import count_open, open_incidents

URLS = [
    "https://fixturehost.org/one",
    "https://fixturehost.org/two",
    "https://fixturehost.org/three",
]


class _Recording:
    """Succeeds, so every URL in the message really is written."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    def post_webpage_item(self, *, url_canonical, title, access_date, tags,
                          item_key=None, **kw):
        self.posted.append(url_canonical)
        return item_key or "KEY"

    def add_tags(self, *a, **k):  # pragma: no cover - no recurring URL here
        raise AssertionError("unexpected")

    def item_exists(self, key):  # pragma: no cover - no claim to resolve
        raise AssertionError("unexpected")


def _capture(db: Path, ledger: Path, *, pinned: str | None, root: str):
    zotero = _Recording()
    capture_message(
        message="see " + " and ".join(URLS),
        project_slug="p",
        context=None,
        today=date(2026, 8, 26),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda url: "t",
        incident_id="inc-1",
        pinned_root=pinned,
        running_root=root,
        ledger_path=ledger,
    )
    return zotero


def test_every_url_in_a_stale_capture_gets_its_own_incident(tmp_path: Path) -> None:
    ledger = tmp_path / "health.db"
    zotero = _capture(tmp_path / "i.db", ledger, pinned="/c/NEW", root="/c/OLD")

    assert len(zotero.posted) == 3, (
        "positive control: all three writes should have been attempted, "
        f"got {zotero.posted}"
    )
    found = open_incidents(ledger)
    assert {i["url"] for i in found} == set(URLS), (
        f"{len(found)} incident(s) recorded for 3 stale writes: "
        f"{[i['url'] for i in found]}"
    )
    assert len({i["incident_id"] for i in found}) == 3, "ids collided"


def test_a_healthy_capture_of_three_urls_still_records_nothing(tmp_path: Path) -> None:
    """Per-mutation ids must not start charging a correct install per URL."""
    ledger = tmp_path / "health.db"
    _capture(tmp_path / "i.db", ledger, pinned="/c/SAME", root="/c/SAME")

    assert count_open(ledger) == 0


def _intent(ledger: Path, *, incident_id: str, url: str) -> None:
    _record_intent(
        ledger_path=ledger,
        incident_id=incident_id,
        url=url,
        running_root="/c/OLD",
        pinned_root="/c/NEW",
        ts="2026-08-26T00:00:00-04:00",
    )


def test_the_same_mutation_recorded_twice_is_still_one_incident(tmp_path: Path) -> None:
    """The id must be derived, not random.

    ON CONFLICT DO NOTHING is what stops a replayed or retried write from
    resurrecting an incident someone already acknowledged. A fresh random id per
    call would make that clause dead code and reopen closed incidents.
    """
    ledger = tmp_path / "health.db"
    _intent(ledger, incident_id="inc-1", url=URLS[0])
    _intent(ledger, incident_id="inc-1", url=URLS[0])

    assert count_open(ledger) == 1


def test_two_captures_of_one_url_are_two_incidents(tmp_path: Path) -> None:
    """Two stale writes of the same source are two things to check, not one."""
    ledger = tmp_path / "health.db"
    _intent(ledger, incident_id="inc-1", url=URLS[0])
    _intent(ledger, incident_id="inc-2", url=URLS[0])

    assert count_open(ledger) == 2
