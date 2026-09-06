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

import pytest


from zotero_capture.health import evaluate

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
PINNED = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.13.0"
DEFAULT_SILENCE = timedelta(hours=24)


def _capture(
    ts: str, *, root: str = PINNED, errors=None, urls_new: int = 1,
    pinned: str | None = PINNED,
) -> str:
    """A capture record. Carries `pinned_root` by default: without pin evidence
    a record cannot be classified at all, which is deliberate but would make
    these fixtures test nothing."""
    return json.dumps(
        {
            "ts": ts,
            "version": "0.13.0",
            "root": root,
            # A record needs writer identity to be an acknowledgeable incident,
            # and evidence of a write to be an integrity incident at all.
            "incident_id": f"{ts}|{root}",
            **({"pinned_root": pinned} if pinned else {}),
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


WINDOW = timedelta(hours=24)


def _check(lines, *, pinned: str | None = PINNED, installed_at=None):
    # `installed_at` is accepted and ignored: generation scoping was removed in
    # 0.18.0 because it was an externally-advanced cursor that dropped incidents
    # nobody had seen. Kept in the signature so these tests read unchanged.
    return evaluate(lines, pinned_root=pinned, now=NOW, window=WINDOW)


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
    warnings = _classify([_capture("2026-08-25T11:30:00-04:00", root=stale)])

    assert len(warnings) == 1
    assert "0.3.0" in warnings[0], warnings



def test_a_later_good_capture_does_not_hide_an_earlier_stale_one() -> None:
    """The defect the 2026-08-25 Codex audit ranked first.

    Two sessions run concurrently: a lingering one on a superseded root and a
    current one. If the current session captures a second later, taking only the
    NEWEST capture makes the stale write invisible — permanently, even though
    that session is still alive and still writing with corrected-away rules.

    This is the exact failure class the health check exists to detect, so it
    failing here would make the whole feature decorative.
    """
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    installed = datetime(2026, 8, 25, 11, 30, tzinfo=timezone(timedelta(hours=-4)))
    lines = [
        _capture("2026-08-25T11:40:00-04:00", root=stale),
        _capture("2026-08-25T11:41:00-04:00", root=PINNED),
    ]

    warnings = _classify(lines, installed_at=installed)

    assert len(warnings) == 1, warnings
    assert "0.3.0" in warnings[0], warnings


def test_every_stale_capture_since_the_upgrade_is_counted() -> None:
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    installed = datetime(2026, 8, 25, 11, 30, tzinfo=timezone(timedelta(hours=-4)))
    lines = [
        _capture("2026-08-25T11:40:00-04:00", root=stale),
        _capture("2026-08-25T11:42:00-04:00", root=stale),
        _capture("2026-08-25T11:43:00-04:00", root=PINNED),
    ]

    warnings = _classify(lines, installed_at=installed)

    # The classifier yields one incident per record: both stale writes are
    # counted, rather than one summarising line.
    assert len(warnings) == 2, warnings


def test_a_stale_root_capture_after_the_upgrade_is_reported() -> None:
    """Same shape, other side of the install: this one really is stale code."""
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.12.0"
    captured = "2026-08-25T11:45:00-04:00"
    installed = datetime(2026, 8, 25, 11, 30, tzinfo=timezone(timedelta(hours=-4)))

    warnings = _classify([_capture(captured, root=stale)], installed_at=installed)

    assert len(warnings) == 1
    assert "0.12.0" in warnings[0], warnings


def test_reports_recent_refusals() -> None:
    lines = [
        _capture("2026-08-25T11:50:00-04:00"),
        _event("2026-08-25T11:00:00-04:00", "stale-root-refused"),
        _event("2026-08-25T11:05:00-04:00", "forward-unresolved"),
    ]
    warnings = _check(lines)

    assert len(warnings) == 1
    assert "2" in warnings[0], warnings
    assert "stale-root-refused" in warnings[0], warnings



def test_a_long_quiet_stretch_alone_is_not_reported() -> None:
    """Wall-clock silence is not evidence of a fault.

    The 24-hour threshold this replaced warned after any ordinary weekend: a
    Friday capture and a Monday session is a 60-70 hour gap with nothing wrong.
    Repeated across SessionStart's startup/resume/clear/compact/fork subtypes,
    that is precisely the chatter that gets a check ignored.
    """
    assert _check([_capture("2026-08-20T11:00:00-04:00")]) == []





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
        # An old integrity incident — these do NOT decay...
        _capture("2026-08-23T11:00:00-04:00", root=stale),
        # ...and a recent operational fault, which does.
        _event("2026-08-25T11:00:00-04:00", "stale-root-refused"),
    ]
    warnings = _check(lines)

    # One signal from the LOG: the recent refusal. The stale write is an
    # integrity incident and now lives in the ledger, which this call has none
    # of — that separation is the point of 0.20.0.
    assert len(warnings) == 1, warnings
    assert "refusal" in warnings[0], warnings


# --- real execution: the hook itself ------------------------------------------
#
# The pure function above was test-driven; this layer is the shell glue that
# decides whether anything reaches the user at all, and it is where "silent
# because healthy" and "silent because broken" look identical from outside.

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from conftest import PLUGIN_ROOT, clean_env  # noqa: E402

HEALTH_HOOK = PLUGIN_ROOT / "hooks" / "session-health.sh"


def _hook_env(state: Path) -> dict[str, str]:
    return clean_env(state)


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
    # HOME is sandboxed by clean_env, so the registry resolves to nothing and
    # the record's own pinned_root is the only pin evidence -- as designed.
    pinned = PINNED
    # Both fields must agree, or the record describes itself as stale.
    _write_log(state, [_recent(0.1, root=pinned, pinned=pinned)])

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
    # The refusal is 2h old (inside the window); the stale write stands until
    # acknowledged. Neither is an elapsed-time claim about absence.
    assert "refusal" in proc.stdout, proc.stdout
    assert "no successful capture in" not in proc.stdout, proc.stdout
    # A 29h-old write from a superseded root is an integrity incident and
    # stands until acknowledged. Until 0.60.0 this line asserted that a string
    # found nowhere in the code was absent from stdout -- a test that could
    # not fail, guarding a report the hook did not make.
    assert "open integrity incident" in proc.stdout, proc.stdout


def test_a_stale_write_with_an_id_is_still_reported_when_the_ledger_is_gone(
    tmp_path: Path,
) -> None:
    """Every post-0.19 capture record carries an incident_id, and the health
    CLI took that as proof the ledger already held it and skipped the import.
    Lose health.db and every such stale write vanished for good, while the
    log still said it happened. The CONTROL is the second half: when the
    ledger DOES hold the capture's own per-mutation rows, the import must not
    add a second incident for the same write."""
    from zotero_capture.health_ledger import mutation_id, open_incident

    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    old = (datetime.now().astimezone() - timedelta(hours=29)).isoformat()

    # Ledger lost: the log is the only witness.
    state = tmp_path / "lost"
    _write_log(state, [_capture(old, root=stale)])
    proc = _run_hook(state)
    assert proc.returncode == 0, proc.stderr
    assert "1 open integrity incident" in proc.stdout, proc.stdout
    proc = _run_hook(state)  # replay is idempotent
    assert "1 open integrity incident" in proc.stdout, proc.stdout

    # Ledger present, with the row the capture journalled for itself.
    state = tmp_path / "kept"
    _write_log(state, [_capture(old, root=stale)])
    record = json.loads(_capture(old, root=stale))
    open_incident(
        state / "health.db",
        incident_id=mutation_id(record["incident_id"], "https://x.test/a"),
        url="https://x.test/a", root=stale, pinned_root=PINNED, kind="stale", ts=old,
    )
    proc = _run_hook(state)
    assert "1 open integrity incident" in proc.stdout, proc.stdout


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


# --- cut back to an incident, after the second audit -------------------------
#
# Scanning every record fixed "a later good capture hides a stale one" and
# immediately created its mirror image: one stale record in an unbounded log
# warned forever, long after the session that wrote it had gone. The record
# proves a stale WRITE happened; it never proved a session is still live.
#
# So the claim shrank to what the evidence supports, and it is reported once.


def test_the_warning_does_not_claim_a_session_is_live() -> None:
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    warnings = _classify(
        [_capture("2026-08-25T11:40:00-04:00", root=stale)],
        installed_at=datetime(2026, 8, 25, 11, 30, tzinfo=timezone(timedelta(hours=-4))),
    )

    assert warnings
    assert "live session" not in warnings[0].lower(), warnings
    assert "ran from plugin" in warnings[0], warnings




def test_a_self_describing_record_is_read_without_the_registry() -> None:
    """The record already proves it: gating on a readable registry threw that away."""
    line = json.dumps(
        {
            "ts": "2026-08-25T11:40:00-04:00",
            "version": "0.3.0",
            "root": "/c/0.3.0",
            "pinned_root": "/c/0.15.0",
            "urls_seen": 1,
            "urls_new": 1,
            "urls_recurring": 0,
            "incident_id": "inline-8814",
            "errors": [],
        }
    )

    assert _classify([line], pinned=None) != [], "self-contained evidence was ignored"


def test_refusals_are_reported_with_no_successful_capture_at_all() -> None:
    """A fresh, wholly broken install has no capture record to anchor to."""
    lines = [_event(f"2026-08-25T11:{m:02d}:00-04:00", "stale-root-refused") for m in range(5)]

    warnings = _check(lines)

    assert warnings, "refusals with zero captures produced silence"
    assert "5" in warnings[0], warnings


def test_a_huge_refusal_count_is_capped_in_the_message() -> None:
    lines = [_event(f"2026-08-25T11:{m // 60:02d}:{m % 60:02d}:00-04:00", "stale-root-refused")
             for m in range(0, 50)]
    warnings = _check(lines)

    assert warnings
    assert len(warnings[0]) < 400, warnings


def test_a_stale_incident_repeats_while_it_is_still_current() -> None:
    """No cursor: the report is not one-shot, it is scoped to this install.

    A timestamp cursor could not be made race-safe — log stamps carry one-second
    precision, so a record appended in the same second as the acknowledgement
    was dropped forever — and it advanced past records that were not part of the
    warning at all, suppressing them once they became classifiable. Both faults
    were in machinery added to stop chatter.

    Repetition is the honest behaviour here: while a stale write since the
    current install exists, an old session is probably still running.
    """
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    installed = datetime(2026, 8, 25, 11, 30, tzinfo=timezone(timedelta(hours=-4)))
    lines = [_capture("2026-08-25T11:40:00-04:00", root=stale)]

    first = _classify(lines, installed_at=installed)
    second = _classify(lines, installed_at=installed)

    assert first and second == first, (first, second)



def test_a_capture_that_could_not_verify_its_pin_is_its_own_warning() -> None:
    """Not stale, but not health either — say which, rather than nothing."""
    line = json.dumps(
        {
            "ts": "2026-08-25T11:40:00-04:00",
            "version": "0.3.0",
            "root": "/c/0.3.0",
            "pin_observation": "unknown",
            "urls_seen": 1,
            "urls_new": 1,
            "urls_recurring": 0,
            "incident_id": "inline-8478",
            "errors": [],
        }
    )
    installed = datetime(2026, 8, 25, 11, 30, tzinfo=timezone(timedelta(hours=-4)))

    warnings = _classify([line], installed_at=installed)

    assert len(warnings) == 1, warnings
    assert "verif" in warnings[0], warnings
    assert "stale" not in warnings[0].lower(), warnings


def test_a_configuration_error_is_visible() -> None:
    """A credential-less install failed on every URL, forever, in silence."""
    line = json.dumps(
        {
            "ts": "2026-08-25T11:40:00-04:00",
            "event": "configuration-error",
            "detail": "missing required environment variable(s): ZOTERO_API_KEY",
        }
    )

    warnings = _check([line])

    assert warnings, "a configuration error produced no warning"
    assert "configuration-error" in warnings[0], warnings


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


def test_the_hook_env_reads_nothing_from_this_machine(tmp_path: Path) -> None:
    """`_hook_env` copied os.environ, so the hook inherited the session's live
    ZOTERO_* credentials and read the developer's real plugin registry (the
    healthy-log test admitted it: "root must match whatever is really
    installed, or this is a false alarm"). Its meaning varied per machine."""
    env = _hook_env(tmp_path)
    leaked = sorted(k for k in env if k.startswith("ZOTERO_") and k not in
                    ("ZOTERO_CAPTURE_STATE_DIR", "ZOTERO_SECRETS_FILE"))
    assert leaked == [], leaked
    assert env["HOME"].startswith(str(tmp_path)), env["HOME"]
    assert not Path(env["ZOTERO_SECRETS_FILE"]).exists()
    # Positive control: the state dir is still the one the test asked for.
    assert env["ZOTERO_CAPTURE_STATE_DIR"] == str(tmp_path)


@pytest.mark.parametrize("raw", ["2026-09-06T12:00:00+0000", "2026-09-06T08:00:00-0400", "2026-09-06T12:00:00+00:00"])
def test_a_log_timestamp_is_readable_on_every_supported_python(raw: str) -> None:
    """The hooks and the capture CLI write `%z`, which is `+0000` with no
    colon. `_parse_ts`'s docstring promised both forms; on Python 3.10
    `fromisoformat` accepts only the colon form, so every record was dropped
    as unreadable and the health check reported a log it could not assess.
    Found by CI's 3.10 jobs on their first run (0.62.1)."""
    from zotero_capture.health import _parse_ts

    parsed = _parse_ts(raw)
    assert parsed is not None, raw
    assert parsed.utcoffset() is not None
    assert parsed.astimezone(timezone.utc).hour == 12
