"""The checker's own failures must not read as health.

Reading capture.log mapped every OSError to exit 0 and silence — permission
denied, the path being a directory, an I/O error — not merely the legitimate
"no log yet" case. A monitor that cannot reach its evidence is not healthy, and
saying nothing is indistinguishable from a clean bill.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import PLUGIN_ROOT

CHECKER = PLUGIN_ROOT / "scripts" / "zotero_capture_health.py"


def _run(state: Path, *args: str, home: Path | None = None):
    env = os.environ.copy()
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(state)
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _record(ts: str, root: str, incident_id: str) -> str:
    return json.dumps(
        {
            "ts": ts, "version": "0.3.0", "root": root,
            "pinned_root": "/c/0.19.0", "project": "p",
            "urls_seen": 1, "urls_new": 1, "urls_recurring": 0,
            "errors": [], "incident_id": incident_id,
        }
    )


def _stale_log(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "capture.log").write_text(
        _record("2026-08-25T11:40:00-0400", "/c/0.3.0", "aaa") + "\n"
    )


def test_a_missing_log_is_healthy_silence(tmp_path: Path) -> None:
    proc = _run(tmp_path / "absent")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_an_unreadable_log_is_not_healthy_silence(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _stale_log(state)
    (state / "capture.log").chmod(0o000)
    try:
        proc = _run(state)
    finally:
        (state / "capture.log").chmod(0o644)

    assert proc.returncode != 0, "an unreadable evidence channel reported success"
    assert proc.stdout == "" or "cannot" in proc.stdout.lower()


def test_a_log_that_is_a_directory_is_not_healthy_silence(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "capture.log").mkdir(parents=True)

    proc = _run(state)

    assert proc.returncode != 0, "a directory in place of the log reported success"


def test_list_incidents_shows_the_ids_that_can_be_acknowledged(tmp_path: Path) -> None:
    """The report aggregates; acknowledgement is per incident. Something has to
    show the individual ids, or --ack silences things nobody was shown."""
    state = tmp_path / "state"
    _stale_log(state)

    proc = _run(state, "--list-incidents")

    assert proc.returncode == 0, proc.stderr
    assert "aaa" in proc.stdout, proc.stdout


def test_ack_by_id_silences_only_that_incident(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "capture.log").write_text(
        _record("2026-08-25T11:40:00-0400", "/c/0.3.0", "aaa") + "\n"
        + _record("2026-08-25T11:41:00-0400", "/c/0.9.0", "bbb") + "\n"
    )

    assert _run(state, "--ack", "aaa").returncode == 0

    after = _run(state)
    assert after.stdout, "acking one id silenced everything"
    assert "0.9.0" in after.stdout, after.stdout
    assert "0.3.0" not in after.stdout, after.stdout


def test_bare_ack_is_not_a_silent_ack_all(tmp_path: Path) -> None:
    """`--ack` with no ids used to clear every incident in the database."""
    state = tmp_path / "state"
    _stale_log(state)

    proc = _run(state, "--ack")

    assert proc.returncode != 0, "bare --ack silently acknowledged everything"


def test_ack_all_is_explicit_and_works(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _stale_log(state)

    assert _run(state, "--ack-all").returncode == 0
    assert _run(state).stdout == ""


def test_a_mistyped_flag_does_not_perform_a_normal_run(tmp_path: Path) -> None:
    """`"--ack" in sys.argv` meant --akc silently did something else entirely."""
    state = tmp_path / "state"
    _stale_log(state)

    proc = _run(state, "--akc")

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)


def test_a_log_of_pure_plaintext_is_not_healthy_silence(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "capture.log").write_text("Traceback (most recent call last):\nnot json\n")

    proc = _run(state)

    assert proc.stdout, "a readable but unparseable log reported perfect health"
    assert "no readable records" in proc.stdout, proc.stdout
