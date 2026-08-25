"""Capture health: say something ONLY when capture is actually broken.

Every serious failure this plugin has had was silent. A stale root wrote junk
for weeks; capture stopped for 29 hours; both were found by an audit rather than
by the plugin noticing. `staleness.py` states the principle — "loud absence
beats quiet corruption" — but nothing was ever watching for the absence.

The log has carried `version`, `root`, `library` and `errors` since 0.12.0, so
this reads what is already recorded rather than adding new bookkeeping.

The hard requirement is silence on a healthy session. A check that chatters gets
ignored, and an ignored check is worse than none — that is precisely how the
measurement canary failed for four releases. The silence test is therefore the
most important one in this file, and the one most likely to rot.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


from zotero_capture.health import evaluate

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
PINNED = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.13.0"
DEFAULT_SILENCE = timedelta(hours=24)


def _capture(ts: str, *, root: str = PINNED, errors=None, urls_new: int = 1) -> str:
    return json.dumps(
        {
            "ts": ts,
            "version": "0.13.0",
            "root": root,
            "project": "demo",
            "urls_seen": 1,
            "urls_new": urls_new,
            "urls_recurring": 0,
            "urls_excluded": 0,
            "errors": errors or [],
        }
    )


def _event(ts: str, event: str) -> str:
    return json.dumps({"ts": ts, "event": event, "self": "/old/root"})


def _check(lines, *, pinned: str | None = PINNED, now=NOW, max_silence=DEFAULT_SILENCE):
    return evaluate(lines, pinned_root=pinned, now=now, max_silence=max_silence)


# --- the requirement that matters most ----------------------------------------


def test_a_healthy_log_says_nothing() -> None:
    """No output on a working session. Chatter here is a real defect."""
    lines = [
        _capture("2026-08-25T11:30:00-04:00"),
        _capture("2026-08-25T11:55:00-04:00"),
    ]
    assert _check(lines) == []


def test_an_empty_log_says_nothing() -> None:
    """A fresh install has never captured; that is not a fault to report."""
    assert _check([]) == []


def test_malformed_lines_do_not_break_the_check() -> None:
    lines = ["not json at all", "", _capture("2026-08-25T11:30:00-04:00"), "{partial"]
    assert _check(lines) == []


# --- the three things worth interrupting for -----------------------------------


def test_reports_a_capture_written_from_an_unpinned_root() -> None:
    """The 29-hour outage and the junk-writing root were both this."""
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    warnings = _check([_capture("2026-08-25T11:30:00-04:00", root=stale)])

    assert len(warnings) == 1
    assert "0.3.0" in warnings[0] and "0.13.0" in warnings[0], warnings


def test_reports_refusals_recorded_since_the_last_capture() -> None:
    lines = [
        _capture("2026-08-25T09:00:00-04:00"),
        _event("2026-08-25T10:00:00-04:00", "stale-root-refused"),
        _event("2026-08-25T10:05:00-04:00", "forward-unresolved"),
    ]
    warnings = _check(lines)

    assert len(warnings) == 1
    assert "2" in warnings[0], warnings
    assert "stale-root-refused" in warnings[0], warnings


def test_ignores_refusals_that_predate_the_last_capture() -> None:
    """Old refusals that were already resolved are not news."""
    lines = [
        _event("2026-08-25T09:00:00-04:00", "stale-root-refused"),
        _capture("2026-08-25T11:00:00-04:00"),
    ]
    assert _check(lines) == []


def test_reports_a_long_silence() -> None:
    warnings = _check([_capture("2026-08-23T11:00:00-04:00")])

    assert len(warnings) == 1
    assert "49" in warnings[0] or "48" in warnings[0], warnings


def test_silence_just_under_the_threshold_is_not_reported() -> None:
    """The boundary decides whether this thing is livable or noisy."""
    assert _check([_capture("2026-08-24T13:00:00-04:00")]) == []


def test_reports_capture_errors_from_the_most_recent_run() -> None:
    lines = [
        _capture(
            "2026-08-25T11:30:00-04:00",
            errors=[{"url": "https://x.test/a", "code": "http", "message": "403"}],
        )
    ]
    warnings = _check(lines)

    assert len(warnings) == 1
    assert "403" in warnings[0] or "http" in warnings[0], warnings


def test_an_unknown_pinned_root_does_not_trigger_a_root_warning() -> None:
    """If the registry cannot be read, do not invent a mismatch."""
    assert _check([_capture("2026-08-25T11:30:00-04:00")], pinned=None) == []


def test_several_problems_are_all_reported() -> None:
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    lines = [
        _capture("2026-08-23T11:00:00-04:00", root=stale),
        _event("2026-08-24T09:00:00-04:00", "stale-root-refused"),
    ]
    warnings = _check(lines)

    assert len(warnings) == 3, warnings


# --- real execution: the hook itself ------------------------------------------
#
# The pure function above was test-driven; this layer is the shell glue that
# decides whether anything reaches the user at all, and it is where "silent
# because healthy" and "silent because broken" look identical from outside.

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from conftest import PLUGIN_ROOT  # noqa: E402

HEALTH_HOOK = PLUGIN_ROOT / "hooks" / "session-health.sh"


def _hook_env(state: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ZOTERO_CAPTURE_DISABLE", None)
    env.pop("ZOTERO_CAPTURE_HEALTH_DISABLE", None)
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(state)
    return env


def _run_hook(state: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        ["bash", str(HEALTH_HOOK)],
        env=env or _hook_env(state),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write_log(state: Path, lines: list[str]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "capture.log").write_text("".join(line + "\n" for line in lines))


def _recent(offset_hours: float = 0.0, **kw) -> str:
    from datetime import datetime as _dt

    ts = (_dt.now().astimezone() - timedelta(hours=offset_hours)).isoformat()
    return _capture(ts, **kw)


def test_hook_is_silent_on_a_healthy_log(tmp_path: Path) -> None:
    """The whole design rests on this. Chatter here and the check gets ignored."""
    state = tmp_path / "state"
    # root must match whatever is really installed, or this is a false alarm
    from zotero_capture_health import _pinned_root

    pinned = _pinned_root() or PINNED
    _write_log(state, [_recent(0.1, root=pinned)])

    proc = _run_hook(state)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"hook spoke on a healthy log: {proc.stdout!r}"


def test_hook_reports_the_outage_shape(tmp_path: Path) -> None:
    """A replay of the real 2026-08-25 incident: stale root, then refusals."""
    state = tmp_path / "state"
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    from datetime import datetime as _dt

    old = (_dt.now().astimezone() - timedelta(hours=29)).isoformat()
    newer = (_dt.now().astimezone() - timedelta(hours=2)).isoformat()
    _write_log(state, [_capture(old, root=stale), _event(newer, "stale-root-refused")])

    proc = _run_hook(state)

    assert proc.returncode == 0, proc.stderr
    assert "0.3.0" in proc.stdout, proc.stdout
    assert "refusal" in proc.stdout, proc.stdout
    assert "29 hours" in proc.stdout, proc.stdout


def test_hook_exits_zero_with_no_log_at_all(tmp_path: Path) -> None:
    """A fresh install must not have its session start broken by this."""
    proc = _run_hook(tmp_path / "nonexistent")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_hook_can_be_switched_off(tmp_path: Path) -> None:
    state = tmp_path / "state"
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    _write_log(state, [_recent(29, root=stale)])
    env = _hook_env(state)
    env["ZOTERO_CAPTURE_HEALTH_DISABLE"] = "1"

    proc = _run_hook(state, env)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_the_hook_script_is_actually_registered() -> None:
    """A hook nobody runs is the failure mode this whole feature exists to fix."""
    import json as _json

    registered = _json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
    commands = [
        h["command"]
        for group in registered["hooks"].get("SessionStart", [])
        for h in group["hooks"]
    ]
    assert any("session-health.sh" in c for c in commands), commands
    assert shutil.which("bash") and HEALTH_HOOK.exists()
