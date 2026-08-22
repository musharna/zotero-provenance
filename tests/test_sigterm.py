"""A hook killed by its timeout must not strand the URL it had claimed.

capture reserves a URL in SQLite before the network call, so that a second
session cannot decide the same URL is new while the POST is in flight. The claim
is released on the way out of any failure — but only for failures that raise.

hooks/capture-stop.sh runs the capture under `timeout 10`, and GNU timeout sends
SIGTERM. Python's default disposition for SIGTERM kills the process outright
without raising, so the release never ran: the row stayed behind with an empty
zotero_key, and every later sighting of that URL took the "claimed by another
session" branch and skipped it. The URL became both uncaptured and undedupable,
permanently, with no signal.

Signal delivery is an OS boundary, so this is exercised by actually sending the
signal to a real subprocess rather than by calling a handler directly.

Reported by an external audit of v0.10.0 (2026-08-22), which also corrected the
mechanism: SIGKILL was never involved, and SIGKILL could not be handled anyway.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from zotero_capture.sqlite_cache import init_db, lookup_url, reserve_url

SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")

# Claims the URL, installs (or does not install) the handler, then blocks the way
# a slow title fetch blocks — inside the window the claim is meant to cover.
CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {scripts!r})
    from datetime import date
    from pathlib import Path
    from zotero_capture.sqlite_cache import init_db, release_url, reserve_url

    db = Path({db!r})
    init_db(db)
    if {install}:
        from zotero_capture.cli import install_termination_handler
        install_termination_handler()
    reserve_url(db, "https://fixturehost.org/slow", date(2026, 8, 22))
    try:
        print("claimed", flush=True)
        time.sleep(30)
    except BaseException:
        release_url(db, "https://fixturehost.org/slow")
        raise
    """
)


def _run_and_terminate(tmp_path: Path, *, install: bool) -> Path:
    db = tmp_path / f"index-{install}.db"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD.format(scripts=SCRIPTS, db=str(db), install=install),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "claimed", "child never took the claim"
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
        proc.kill()
        pytest.fail("child ignored SIGTERM")
    return db


def _claim_is_stranded(db: Path) -> bool:
    row = lookup_url(db, "https://fixturehost.org/slow")
    return row is not None and not row["zotero_key"]


def test_sigterm_without_the_handler_strands_the_claim(tmp_path: Path):
    """Negative control: the bug, so the test below is known to be able to fail.

    Without this, a handler that did nothing would still let the next test pass
    if the release happened for some unrelated reason.
    """
    db = _run_and_terminate(tmp_path, install=False)
    assert _claim_is_stranded(db), "expected the unhandled SIGTERM to strand the claim"


def test_sigterm_with_the_handler_releases_the_claim(tmp_path: Path):
    db = _run_and_terminate(tmp_path, install=True)
    assert not _claim_is_stranded(db), "the claim outlived the terminated hook"


def test_a_completed_claim_is_never_released(tmp_path: Path):
    """Positive control: release must not undo work that actually succeeded."""
    db = tmp_path / "done.db"
    init_db(db)
    from datetime import date

    from zotero_capture.sqlite_cache import release_url, set_zotero_key

    reserve_url(db, "https://fixturehost.org/done", date(2026, 8, 22))
    set_zotero_key(db, "https://fixturehost.org/done", "KEY1")
    release_url(db, "https://fixturehost.org/done")
    row = lookup_url(db, "https://fixturehost.org/done")
    assert row is not None and row["zotero_key"] == "KEY1"
