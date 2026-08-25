"""An integrity incident must be individually identifiable and individually clearable.

Acknowledgement keyed on `timestamp-to-the-second | root` aliased distinct
incidents: a stale write and an unverifiable write from the same root in the
same second produced ONE key, so acknowledging either silenced both — forever,
without the second ever having been shown. Two stale writes in the same second
collided the same way.

Identity therefore comes from the writer, not from a guess reconstructed after
the fact. Every capture emits a unique id, and a record without one is not
classified at all — the same rule already applied to records with no pin
evidence, so it costs nothing new.

Separately, "capture-shaped" is not "wrote a row". A record that captured
nothing became a permanent integrity incident requiring acknowledgement, which
is a nag about an event that never happened.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from zotero_capture.health import evaluate, incidents

TZ = timezone(timedelta(hours=-4))
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=TZ)
WINDOW = timedelta(hours=24)
PINNED = "/c/0.19.0"


def _cap(ts: str, *, root: str, pinned: str | None = PINNED, incident_id: str | None = "i1",
         urls_new: int = 1, recurring: int = 0, pin_observation: str | None = None) -> str:
    obj = {
        "ts": ts, "version": "x", "root": root, "project": "p",
        "urls_seen": urls_new + recurring, "urls_new": urls_new,
        "urls_recurring": recurring, "errors": [],
    }
    if pinned is not None:
        obj["pinned_root"] = pinned
    if incident_id is not None:
        obj["incident_id"] = incident_id
    if pin_observation is not None:
        obj["pin_observation"] = pin_observation
        obj.pop("pinned_root", None)
    return json.dumps(obj)


def _check(lines, acknowledged=frozenset()):
    return evaluate(lines, pinned_root=PINNED, now=NOW, window=WINDOW,
                    acknowledged=acknowledged)


def test_two_incidents_in_one_second_are_two_incidents() -> None:
    """The aliasing defect: same root, same second, different faults."""
    stale = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", incident_id="a")
    unver = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", incident_id="b",
                 pin_observation="unknown")

    found = incidents([stale, unver], pinned_root=PINNED)

    assert len({i["id"] for i in found}) == 2, found


def test_acknowledging_one_does_not_silence_the_other() -> None:
    stale = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", incident_id="a")
    unver = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", incident_id="b",
                 pin_observation="unknown")

    warnings = _classify([stale, unver], acknowledged=frozenset({"a"}))

    assert warnings, "acknowledging one incident silenced the other"
    assert any("unverified" in w for w in warnings), warnings


def test_a_record_without_an_id_is_not_classified() -> None:
    """Same rule as a record with no pin evidence: it cannot be acknowledged,
    so it must not be reported — an unclearable nag is worse than silence."""
    legacy = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", incident_id=None)

    assert _check([legacy]) == [], "an unacknowledgeable incident was reported"


def test_a_capture_that_wrote_nothing_is_not_an_integrity_incident() -> None:
    """Nothing was written, so there is no row to check and nothing to nag about."""
    nothing = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", urls_new=0,
                   recurring=0, pin_observation="unknown")

    assert _check([nothing]) == [], "a zero-write record became a permanent incident"


def test_a_recurring_write_still_counts_as_a_write() -> None:
    """Re-tagging an existing row still touched the library."""
    recur = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", urls_new=0, recurring=1)

    assert _classify([recur]), "a recurring write was treated as writing nothing"


def test_incidents_reports_what_ack_would_silence() -> None:
    """--list-incidents must show exactly what --ack-all would clear."""
    a = _cap("2026-08-25T11:00:00-04:00", root="/c/OLD", incident_id="a")
    b = _cap("2026-08-25T11:30:00-04:00", root="/c/OTHER", incident_id="b")

    found = incidents([a, b], pinned_root=PINNED)

    assert {i["id"] for i in found} == {"a", "b"}
    for item in found:
        assert item["kind"] in {"stale", "unverified"}
        assert item["root"] and item["ts"]


def test_the_warning_names_a_command_that_actually_works(tmp_path) -> None:
    """0.19.0 shipped advice to run `--ack`, which by then exited 2.

    The advice now lives on the ledger-backed report, since that is what names
    open incidents. Advice that fails when followed is worse than none.
    """
    from datetime import datetime, timedelta, timezone

    from zotero_capture.health import evaluate
    from zotero_capture.health_ledger import open_incident

    ledger = tmp_path / "health.db"
    open_incident(ledger, incident_id="abc123", url="u", root="/c/OLD",
                  pinned_root=PINNED, kind="stale", ts="2026-08-25T11:00:00-04:00")

    warnings = evaluate([], pinned_root=PINNED,
                        now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone(timedelta(hours=-4))),
                        window=timedelta(hours=24), ledger_path=ledger)

    assert warnings
    text = " ".join(warnings)
    assert "--list-incidents" in text, text
    assert "--ack <id>" in text, text
    assert "abc123" in text, text


def _classify(lines, acknowledged=frozenset(), **_ignored):
    from zotero_capture.health import incidents as _incidents

    return [
        f"{i['kind']} capture(s) ran from plugin {i['root']} [{i['id']}]"
        for i in _incidents(lines, pinned_root=PINNED)
        if i["id"] not in acknowledged
    ]
