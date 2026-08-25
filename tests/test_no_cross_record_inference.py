"""No record's meaning may depend on another record.

Four audits found four defects in this check, and they share one mechanism.
A later successful capture cleared an earlier refusal. The install timestamp
scoped away a stale write. An acknowledgement cursor suppressed by timestamp.
Each fix corrected one instance; the mechanism survived and produced the next.

The mechanism is cross-record inference. Removing it removes the class:

- refusals and capture errors are reported within a recency WINDOW. Timing a
  presence is sound — "3 refusals in the last day" is checkable and decays on
  its own. Timing an ABSENCE is what failed before, and is gone.
- stale and unverified writes are integrity incidents. They are reported until
  the user acknowledges them explicitly, never automatically: an incident that
  nothing has repaired is still true a week later, and only a person can say it
  has been handled.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from zotero_capture.health import evaluate, incident_keys

TZ = timezone(timedelta(hours=-4))
NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=TZ)
PINNED = "/c/0.18.0"
WINDOW = timedelta(hours=24)


def _cap(ts: str, *, root: str = PINNED, pinned: str | None = PINNED, errors=None,
         incident_id: str | None = None) -> str:
    """A capture record. Carries an `incident_id` by default — without one a
    record cannot be acknowledged individually, so it is not classified."""
    obj = {
        "ts": ts, "version": "x", "root": root, "project": "p",
        "urls_seen": 1, "urls_new": 1, "urls_recurring": 0, "errors": errors or [],
        "incident_id": incident_id or f"{ts}:{root}",
    }
    if pinned is not None:
        obj["pinned_root"] = pinned
    return json.dumps(obj)


def _ev(ts: str, name: str) -> str:
    return json.dumps({"ts": ts, "event": name, "self": "/old"})


def _check(lines, *, acknowledged=frozenset(), now=NOW):
    return evaluate(
        lines, pinned_root=PINNED, now=now, window=WINDOW, acknowledged=acknowledged
    )


def test_a_good_capture_does_not_clear_another_sessions_refusal() -> None:
    """The recurrence: fixed for stale captures in 0.15.0, still live for refusals."""
    lines = [
        _ev("2026-08-25T11:40:00-04:00", "forward-unresolved"),
        _cap("2026-08-25T11:41:00-04:00"),
    ]

    warnings = _check(lines)

    assert warnings, "a concurrent success erased a stranded session's refusal"
    assert any("refusal" in w for w in warnings), warnings


def test_append_order_within_one_second_cannot_hide_a_refusal() -> None:
    """Second-precision stamps meant `refusal > capture` lost the ordering."""
    lines = [
        _cap("2026-08-25T12:00:00-04:00"),
        _ev("2026-08-25T12:00:00-04:00", "forward-unresolved"),
    ]

    assert _check(lines), "same-second refusal was swallowed"


def test_a_stale_write_is_reported_regardless_of_when_the_version_was_installed() -> None:
    """installed_at was an externally-advanced cursor that dropped incidents."""
    lines = [_cap("2026-08-20T09:00:00-04:00", root="/c/0.3.0", pinned=PINNED)]

    warnings = _classify(lines)

    assert warnings, "an old stale write was retired by an upgrade"
    assert "0.3.0" in warnings[0], warnings


def test_a_stale_write_stops_only_when_acknowledged() -> None:
    """Only a person can say an integrity incident has been handled."""
    lines = [_cap("2026-08-20T09:00:00-04:00", root="/c/0.3.0", pinned=PINNED)]

    keys = incident_keys(lines, pinned_root=PINNED)
    assert keys, "no acknowledgeable incident was produced"

    assert _classify(lines) != []
    assert _classify(lines, acknowledged=keys) == []


def test_acknowledging_one_incident_does_not_hide_a_different_one() -> None:
    old = _cap("2026-08-20T09:00:00-04:00", root="/c/0.3.0", pinned=PINNED)
    new = _cap("2026-08-24T09:00:00-04:00", root="/c/0.9.0", pinned=PINNED)

    acked = incident_keys([old], pinned_root=PINNED)

    warnings = _classify([old, new], acknowledged=acked)
    assert warnings, "acknowledging one incident silenced another"
    assert "0.9.0" in warnings[0] and "0.3.0" not in warnings[0], warnings


def test_an_old_refusal_ages_out_of_the_window() -> None:
    """Operational noise decays; integrity incidents do not."""
    assert _check([_ev("2026-08-20T09:00:00-04:00", "forward-unresolved")]) == []


def test_a_recent_refusal_is_reported_with_no_capture_anywhere() -> None:
    assert _check([_ev("2026-08-25T11:00:00-04:00", "configuration-error")])


def test_capture_errors_are_read_from_every_recent_record_not_just_the_last() -> None:
    """"The LAST capture's errors" was itself cross-record inference."""
    lines = [
        _cap("2026-08-25T11:00:00-04:00", errors=[{"code": "http", "message": "403", "url": "u"}]),
        _cap("2026-08-25T11:30:00-04:00"),
    ]

    warnings = _check(lines)

    assert any("403" in w or "http" in w for w in warnings), warnings


def test_a_record_with_no_pin_evidence_is_not_classified() -> None:
    """Found by running the check against the real log, not by a test.

    Captures written before 0.15.0 carry no `pinned_root`. Comparing them to
    TODAY's pin said eighteen perfectly correct captures were stale — they ran
    when their own root WAS the installed one, and only look wrong because the
    pin moved afterwards. That is precisely the cross-record inference this
    release removes, smuggled in through a legacy fallback.

    A record with no pin evidence proves nothing about staleness, so it is not
    classified. The cost is silence about writes older than the field; the
    field exists so that everything after it can be judged on its own.
    """
    legacy = json.dumps(
        {
            "ts": "2026-08-25T08:00:00-04:00", "version": "0.14.1",
            "root": "/c/0.14.1", "project": "p",
            "urls_seen": 1, "urls_new": 1, "errors": [],
        }
    )

    assert _check([legacy]) == [], "a legacy record was judged against today's pin"


def test_a_record_with_pin_evidence_is_still_classified() -> None:
    """Positive control: the fix above must not disable the signal."""
    current = _cap("2026-08-25T08:00:00-04:00", root="/c/0.3.0", pinned=PINNED)
    assert _classify([current]), "the stale signal stopped working"


def test_reporting_is_bounded_no_matter_how_many_incidents_exist(tmp_path) -> None:
    """The reason integrity moved to a ledger.

    Replaying an unbounded log and filtering acknowledged ids needs an unbounded
    set to filter with — there is no bounded lossless version. A ledger answers
    "how many are open" with a count and shows a handful, so the SessionStart
    path stays flat however bad things are. The first version of this test
    measured `evaluate()` over all-HEALTHY records, so nothing accumulated and
    it reported a flat peak that meant nothing.
    """
    import tracemalloc

    from zotero_capture.health_ledger import open_incident

    ledger = tmp_path / "health.db"
    for i in range(5_000):
        open_incident(ledger, incident_id=f"i{i}", url="u", root="/c/OLD",
                      pinned_root=PINNED, kind="stale",
                      ts="2026-08-25T11:00:00-04:00")

    tracemalloc.start()
    try:
        warnings = evaluate([], pinned_root=PINNED, now=NOW, window=WINDOW,
                            ledger_path=ledger)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert warnings, "positive control: 5000 open incidents should be reported"
    assert "5000" in warnings[0], warnings
    assert peak < 1024 * 1024, f"held {peak / 1024:.0f} KB to report 5000 incidents"



# --- 0.20.0: integrity moved from log-replay to the ledger --------------------
#
# `evaluate()` no longer classifies integrity from log records; incidents are
# written to a ledger BEFORE the mutation they describe, because the log could
# only ever say what already finished. These tests still assert the thing that
# matters — that a record's own contents decide, with no reference to any other
# record — so they now exercise `incidents()`, the classifier that FEEDS the
# ledger, instead of the reporter that reads it.


def _classify(lines, acknowledged=frozenset(), **_ignored):
    from zotero_capture.health import incidents as _incidents

    return [
        f"{i['kind']} capture(s) ran from plugin {i['root']} [{i['id']}]"
        for i in _incidents(lines, pinned_root=PINNED)
        if i["id"] not in acknowledged
    ]
