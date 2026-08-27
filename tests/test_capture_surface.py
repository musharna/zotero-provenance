"""Which surface drove a capture is recorded, so the claim stops being a belief.

From its first commit the README carried this as a documented limitation:

    Sessions bridged with `/remote-control` do not fire local `Stop` hooks, so
    nothing is captured in those sessions.

It traced back to "Add README" with no evidence behind it, and it is false.
Remote Control bridges a session that goes on running locally -- the docs say
hooks "run wherever Claude Code runs: sessions in the terminal, IDE extensions,
the Desktop app, and Claude Code on the web all fire the same hook events" --
and `CLAUDE_CODE_BRIDGE_SESSION_ID` is set on the LOCAL session for as long as
the bridge is attached.

It was disproved by execution, not by reading: a session with that variable set
captured its own citations into the library, five URLs from one assistant turn,
all dated the same day. The real limitation is a different one -- a CLOUD
session does not read local `~/.claude/settings.json`, so a user-scope plugin is
not installed there at all.

Recording the surface makes the next such claim a query instead of an
assumption. Only the class is stored, never the bridge session id.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import PLUGIN_ROOT

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from zotero_capture.capture import CaptureResult  # noqa: E402
from zotero_capture.cli import _emit_log, _surface  # noqa: E402

BRIDGE = "CLAUDE_CODE_BRIDGE_SESSION_ID"
REMOTE = "CLAUDE_CODE_REMOTE"


def test_a_local_cli_session_is_local() -> None:
    assert _surface({}) == "local"


def test_a_bridged_session_is_bridged() -> None:
    assert _surface({BRIDGE: "session_01ABC"}) == "bridged"


def test_a_remote_web_environment_is_remote() -> None:
    assert _surface({REMOTE: "true"}) == "remote"


def test_bridged_wins_when_both_are_set() -> None:
    """The interesting fact about a bridged session is that it is bridged."""
    assert _surface({BRIDGE: "session_01ABC", REMOTE: "true"}) == "bridged"


def test_an_unset_remote_flag_is_not_remote() -> None:
    """Negative control. `CLAUDE_CODE_REMOTE` is documented as set to the string
    "true" or not set at all; treating mere presence as truth would classify
    every local session as remote."""
    assert _surface({REMOTE: ""}) == "local"
    assert _surface({REMOTE: "false"}) == "local"


def test_the_capture_record_carries_the_surface(tmp_path: Path, monkeypatch) -> None:
    """The field reaches the log, not just the helper.

    A unit-tested helper nothing calls is the shape that let dev/measure_extraction
    swap a regex the code no longer read.
    """
    monkeypatch.setenv(BRIDGE, "session_01ABC")
    log = tmp_path / "capture.log"

    _emit_log(log, project="p", context=None, result=CaptureResult(), latency_ms=1)

    record = json.loads(log.read_text().splitlines()[-1])
    assert record["surface"] == "bridged"


def test_the_surface_is_local_without_the_bridge_variable(
    tmp_path: Path, monkeypatch
) -> None:
    """Positive control for the test above: if the field were hardcoded to
    "bridged", or the env were ignored, that test would pass anyway."""
    monkeypatch.delenv(BRIDGE, raising=False)
    monkeypatch.delenv(REMOTE, raising=False)
    log = tmp_path / "capture.log"

    _emit_log(log, project="p", context=None, result=CaptureResult(), latency_ms=1)

    assert json.loads(log.read_text().splitlines()[-1])["surface"] == "local"
