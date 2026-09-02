"""A refusal was summarised by truncating the sentence that explained it.

The SessionStart health line reported four refusals on 2026-09-01 like this:

    4 refusal(s) recorded in the last 24 hours (refusing to capture: this
    session is running plugin version , refusing to capture: this session is
    running plugin version , refusing to capture: this session is running
    plugin version ; newest 2026-09-01T20:05:14-04:00)

Three identical reasons, each naming no version at all. The records were not
identical: they were 0.42.0-vs-0.41.0, 0.44.0-vs-0.45.0 and 0.45.0-vs-0.46.0.
`refusal_kinds` is a SET, so it deduplicated on the full 409-character message
and correctly kept three entries; the renderer then cut each at `k[:60]`, and
"refusing to capture: this session is running plugin version " is exactly 60
characters. The cut landed on the only bytes that differed.

`stale_reason` says in its own docstring why those bytes exist: "'your plugin
is stale' is not actionable, while 'running 0.3.0, 0.11.7 is installed' says
exactly what happened and implies the fix." The summary deleted precisely the
property the message was written to have.

The width is not the defect. A set named `kinds` was being fed a REASON. A kind
is a short closed label -- `MAX_DISTINCT_KINDS = 16` and `MAX_KINDS_SHOWN = 3`
only make sense for one -- and the hook writer already emits exactly that in
`event`. The capture path (capture.py) recorded unbounded prose and no label,
so `record.get("event") or record.get("refused")` fell through to the sentence.
Two writers, one fact, two shapes: the fifth appearance of that class here.

Truncation existed only to make prose survive a slot it never belonged in, so
the fix DELETES the truncation rather than widening it. The full reason still
goes to capture.log, complete, which is where a diagnosis actually reads it.

Why the suite did not catch it: every existing refusal fixture in
tests/test_health.py builds `_event(ts, "stale-root-refused")` -- the hook shape,
which has a kind and works. Not one test fed the shape the capture path actually
writes. The fixtures only ever produced the record that already passed.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

from zotero_capture import capture as capture_mod
from zotero_capture.health import evaluate

WINDOW = timedelta(hours=24)
NOW = "2026-09-01T20:05:14-04:00"
NOW_DT = datetime.fromisoformat(NOW)

# The real message, at its real length. A shortened stand-in would not reach the
# truncation and the test would pass on the broken code.
from zotero_capture.staleness import stale_reason  # noqa: E402


def _refusal(ts: str, running: str, installed: str) -> str:
    """A refusal as the CAPTURE path writes it, not as the hook writes it."""
    reason = stale_reason(running, installed)
    assert reason, "positive control: these versions must actually refuse"
    return json.dumps(
        {
            "ts": ts,
            "version": running,
            "root": "/some/root",
            "refused": reason,
            "refused_kind": capture_mod.STALE_ROOT_REFUSED,
        }
    )


def _legacy_refusal(ts: str, running: str, installed: str) -> str:
    """The same thing as written BEFORE this fix: prose, no label. These are
    already in capture.log and must keep counting as refusals."""
    return json.dumps(
        {"ts": ts, "version": running, "root": "/r", "refused": stale_reason(running, installed)}
    )


def _warn(lines: list[str]) -> list[str]:
    return evaluate(lines, window=WINDOW, now=NOW_DT, pinned_root=None)


def _refusal_line(lines: list[str]) -> str:
    hits = [w for w in _warn(lines) if "refusal" in w]
    assert hits, f"no refusal warning produced: {_warn(lines)}"
    return hits[0]


def test_the_summary_never_prints_the_reason_prose() -> None:
    """The defect, stated directly. Any fragment of the sentence in the summary
    means prose reached a slot rendered at fixed width."""
    line = _refusal_line([_refusal(NOW, "0.45.0", "0.46.0")])
    assert "refusing to capture" not in line, line
    assert "plugin version" not in line, line


def test_three_different_refusals_are_not_rendered_as_the_same_string() -> None:
    """What the user was actually shown. Three distinct incidents became three
    copies of one truncated fragment."""
    lines = [
        _refusal("2026-09-01T09:34:52-04:00", "0.42.0", "0.41.0"),
        _refusal("2026-09-01T16:33:41-04:00", "0.44.0", "0.45.0"),
        _refusal("2026-09-01T20:05:14-04:00", "0.45.0", "0.46.0"),
    ]
    line = _refusal_line(lines)
    assert "3 refusal(s)" in line, line
    # They share one CLASS, so the class is named once -- not repeated three
    # times as if it were three different findings.
    assert line.count(capture_mod.STALE_ROOT_REFUSED) == 1, line


def test_the_class_is_named() -> None:
    line = _refusal_line([_refusal(NOW, "0.45.0", "0.46.0")])
    assert capture_mod.STALE_ROOT_REFUSED in line, line


def test_a_hook_event_refusal_still_names_its_kind() -> None:
    """Positive control for the OTHER writer. A fix that only handled the
    capture shape would break the shape that already worked."""
    line = _refusal_line([json.dumps({"ts": NOW, "event": "stale-root-refused", "self": "/old"})])
    assert "stale-root-refused" in line, line


def test_a_legacy_unlabelled_refusal_is_still_counted() -> None:
    """The records already on disk have no kind. Losing the label is
    acceptable; silently dropping the refusal is not -- that would turn a
    noisy alert into a missing one, which is the failure this whole module
    exists to prevent."""
    line = _refusal_line([_legacy_refusal(NOW, "0.45.0", "0.46.0")])
    assert "1 refusal(s)" in line, line
    assert "refusing to capture" not in line, line


# --- the writer must label every refusal --------------------------------------


def _assignments_to_refused() -> list[tuple[str, int]]:
    """Every place capture.py sets `result.refused`, with its enclosing function.

    Derived from the assignment itself, not from a list of the sites known
    today. A guard that named capture.py:257 and capture.py:269 could not fail
    on a third refusal added tomorrow -- which is the same defect in a guard
    that the guard exists to prevent in the code.
    """
    tree = ast.parse(Path(capture_mod.__file__).read_text())
    out: list[tuple[str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "refused":
                    out.append((fn.name, node.lineno))
    return out


def test_the_derivation_finds_the_refusal_sites() -> None:
    """Positive control. If this found nothing, every assertion below would
    pass while checking nothing -- exactly how the previous guards read green."""
    assert _assignments_to_refused(), "no refusal site found; the guard is vacuous"


def test_every_refusal_is_written_through_one_place() -> None:
    """A refusal carries a kind AND a reason, and two fields that must agree
    cannot be set at two sites without drifting apart. There are already four
    stale-second-copy defects in this repository and zero missing guards."""
    sites = _assignments_to_refused()
    stray = [(fn, ln) for fn, ln in sites if fn != "_refuse"]
    assert not stray, (
        f"result.refused is set outside _refuse() at {stray}; a refusal set "
        f"there carries no kind and the summary cannot name its class"
    )


def test_the_helper_sets_both_halves() -> None:
    """...and the one place must set both, or the guard above just relocates
    the bug into the helper."""
    from zotero_capture.capture import CaptureResult, _refuse

    r = CaptureResult()
    _refuse(r, capture_mod.STALE_ROOT_REFUSED, "because")
    assert r.refused == "because"
    assert r.refused_kind == capture_mod.STALE_ROOT_REFUSED


def test_the_cli_logs_the_kind_beside_the_reason() -> None:
    """Accepting a field nobody writes to the log is the same dead flag one
    layer along -- the defect class this repository has shipped four times."""
    cli = Path(capture_mod.__file__).with_name("cli.py").read_text()
    assert '"refused_kind"' in cli, "the kind never reaches the log"
