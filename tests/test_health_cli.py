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


def _stale_log(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "capture.log").write_text(
        json.dumps(
            {
                "ts": "2026-08-25T11:40:00-0400", "version": "0.3.0",
                "root": "/c/0.3.0", "pinned_root": "/c/0.18.0",
                "urls_seen": 1, "urls_new": 1, "errors": [],
            }
        )
        + "\n"
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


def test_ack_silences_the_incident_it_was_shown(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _stale_log(state)

    before = _run(state)
    assert before.returncode == 0 and before.stdout, before.stdout

    acked = _run(state, "--ack")
    assert acked.returncode == 0, acked.stderr

    after = _run(state)
    assert after.returncode == 0, after.stderr
    assert after.stdout == "", f"--ack did not silence it: {after.stdout!r}"


def test_ack_does_not_silence_an_incident_it_never_saw(tmp_path: Path) -> None:
    """Acknowledgement is per incident, not a global mute."""
    state = tmp_path / "state"
    _stale_log(state)
    _run(state, "--ack")

    with (state / "capture.log").open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": "2026-08-25T13:00:00-0400", "version": "0.9.0",
                    "root": "/c/0.9.0", "pinned_root": "/c/0.18.0",
                    "urls_seen": 1, "urls_new": 1, "errors": [],
                }
            )
            + "\n"
        )

    proc = _run(state)
    assert proc.stdout, "a new incident was muted by an earlier acknowledgement"
    assert "0.9.0" in proc.stdout
