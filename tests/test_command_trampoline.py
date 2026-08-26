"""The slash commands need the trampoline too, and never had it.

0.13.0 gave the capture and health HOOKS a trampoline so a session pinned to a
superseded root delegates to the installed one instead of running rules a later
release corrected. The COMMAND path was never covered. `commands/triage.md`
invokes ${CLAUDE_PLUGIN_ROOT}/hooks/run-python.sh, which was three lines and no
delegation -- so a superseded session ran old logic that mutates the library,
and unlike capture there is no availability argument for letting it through.

run-python.sh also differs from the hooks in one way that matters: it is given
the script to run as an ARGUMENT, qualified by the caller's root. Forwarding
that unchanged would run the superseded root's code from the installed root's
launcher, which delegates nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from conftest import PLUGIN_ROOT

CACHE_REL = Path(".claude/plugins/cache/zotero-provenance/zotero-provenance")


def _build(tmp_path: Path, *, mine: str, pinned: str) -> dict:
    home = tmp_path / "home"
    cache = home / CACHE_REL
    mine_root, pinned_root = cache / mine, cache / pinned
    (mine_root / "hooks").mkdir(parents=True, exist_ok=True)
    for name in ("run-python.sh", "lib.sh"):
        shutil.copy(PLUGIN_ROOT / "hooks" / name, mine_root / "hooks" / name)
    (mine_root / "scripts").mkdir(exist_ok=True)
    (mine_root / "scripts" / "zotero_capture_main.py").write_text("# old\n")

    record = tmp_path / "args.txt"
    if pinned_root != mine_root:
        (pinned_root / "hooks").mkdir(parents=True, exist_ok=True)
        stub = pinned_root / "hooks" / "run-python.sh"
        stub.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\nexit 0\n'
        )
        stub.chmod(0o755)
        (pinned_root / "scripts").mkdir(exist_ok=True)
        (pinned_root / "scripts" / "zotero_capture_main.py").write_text("# new\n")

    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({
        "version": 2,
        "plugins": {"zotero-provenance@zotero-provenance": [
            {"scope": "user", "installPath": str(pinned_root), "version": pinned}
        ]},
    }))
    return {"home": home, "mine": mine_root, "pinned": pinned_root, "record": record}


def _run(setup: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for var in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE",
                "ZOTERO_WEBSOURCES_COLLECTION_KEY", "ZP_FORWARDED_FROM"):
        env.pop(var, None)
    env["HOME"] = str(setup["home"])
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(tmp_path / "state")
    env["ZOTERO_SECRETS_FILE"] = str(tmp_path / "absent.env")
    return subprocess.run(
        ["bash", str(setup["mine"] / "hooks" / "run-python.sh"),
         str(setup["mine"] / "scripts" / "zotero_capture_main.py"), "--triage", "u"],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_a_superseded_root_forwards_the_command(tmp_path: Path) -> None:
    setup = _build(tmp_path, mine="0.1.0", pinned="0.2.0")

    _run(setup, tmp_path)

    assert setup["record"].exists(), "the superseded root ran the command itself"


def test_the_forwarded_command_runs_the_INSTALLED_root_s_script(
    tmp_path: Path,
) -> None:
    """Forwarding the caller's own script path delegates nothing at all."""
    setup = _build(tmp_path, mine="0.1.0", pinned="0.2.0")

    _run(setup, tmp_path)

    args = setup["record"].read_text().splitlines()
    assert args[1:] == ["--triage", "u"], args
    assert args[0] == str(setup["pinned"] / "scripts" / "zotero_capture_main.py"), (
        f"the installed launcher was handed the superseded root's script: {args[0]}"
    )


def test_the_installed_root_runs_its_own_command(tmp_path: Path) -> None:
    """Positive control: the ordinary case must not forward anywhere."""
    setup = _build(tmp_path, mine="0.2.0", pinned="0.2.0")

    proc = _run(setup, tmp_path)

    assert not setup["record"].exists()
    # It got as far as trying to run the script, rather than refusing.
    assert "cannot resolve" not in proc.stderr, proc.stderr
