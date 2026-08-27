"""The harness that keeps a probe out of production state.

On 2026-08-26 a probe drove the trampoline's loop-refusal branch deliberately --
a negative control, the right instinct -- and minted a real `forward-loop-refused`
record in the live capture.log, which then greeted every new session as a capture
fault for 24 hours. The probe had closed the network channel and left the state
directory pointed at production. `ZOTERO_CAPTURE_STATE_DIR` already existed on
every hook; nothing needed building, and the knob simply went unused.

The suite was never the exposure. It was measured clean the same day: run with
the state directory redirected, 676 tests wrote nothing there, with a positive
control confirming the harness could have seen a write. Hand-run probes against
live roots are the gap, because they are the thing with no harness at all.

So these tests exercise the two dev scripts the way the scripts exercise a root:
every negative assertion is paired with a positive control, and every guard is
built broken once to watch it fail. A detector nobody has seen detect is not
evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from conftest import PLUGIN_ROOT

PROBE = PLUGIN_ROOT / "dev" / "probe_root.sh"
BACKPORT = PLUGIN_ROOT / "dev" / "backport_trampoline.sh"
CACHE_REL = Path(".claude/plugins/cache/zotero-provenance/zotero-provenance")
HOOKS = ("capture-prompt.sh", "capture-stop.sh", "session-health.sh", "run-python.sh")

PLAIN_LAUNCHER = """#!/usr/bin/env bash
set -euo pipefail
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib.sh"
zp_load_secrets
exec "$(zp_python)" "$@"
"""


def _make_root(
    cache: Path,
    version: str,
    *,
    trampolined: bool = True,
    shell_honours: bool = True,
    python_honours: bool = True,
) -> Path:
    """A plugin root, optionally broken in one specific way."""
    root = cache / version
    (root / "hooks").mkdir(parents=True, exist_ok=True)
    for name in (*HOOKS, "lib.sh"):
        shutil.copy(PLUGIN_ROOT / "hooks" / name, root / "hooks" / name)
        (root / "hooks" / name).chmod(0o755)

    if not trampolined:
        launcher = root / "hooks" / "run-python.sh"
        launcher.write_text(PLAIN_LAUNCHER)
        launcher.chmod(0o755)

    if not shell_honours:
        launcher = root / "hooks" / "run-python.sh"
        launcher.write_text(
            launcher.read_text().replace(
                'ZP_LOG="${ZOTERO_CAPTURE_STATE_DIR:'
                '-${XDG_STATE_HOME:-$HOME/.local/state}/zotero-provenance}/capture.log"',
                'ZP_LOG="/tmp/hardcoded-zp/capture.log"',
            )
        )

    pkg = root / "scripts" / "zotero_capture"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    if python_honours:
        shutil.copy(
            PLUGIN_ROOT / "scripts" / "zotero_capture" / "config.py", pkg / "config.py"
        )
    else:
        (pkg / "config.py").write_text(
            "from pathlib import Path\n"
            "def _state_dir(env):\n"
            "    return Path('/tmp/hardcoded-zp')\n"
        )
    return root


def _write_registry(home: Path, pinned: Path) -> None:
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "zotero-provenance@zotero-provenance": [
                        {"scope": "user", "installPath": str(pinned)}
                    ]
                },
            }
        )
    )


def _env(home: Path, protected: Path) -> dict:
    env = os.environ.copy()
    for var in (
        "ZOTERO_API_KEY",
        "ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_TYPE",
        "ZOTERO_WEBSOURCES_COLLECTION_KEY",
        "ZP_FORWARDED_FROM",
    ):
        env.pop(var, None)
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(protected)
    env["ZOTERO_SECRETS_FILE"] = str(home / "absent.env")
    return env


def _run(script: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# --- probe_root.sh ----------------------------------------------------------


def test_the_control_proves_the_guard_can_see_a_write(tmp_path: Path) -> None:
    """--control is the harness checking itself, and must pass."""
    env = _env(tmp_path / "home", tmp_path / "protected")

    proc = _run(PROBE, "--control", env=env)

    assert proc.returncode == 0, proc.stderr
    assert "control ok" in proc.stdout


def test_a_root_whose_shell_ignores_the_redirect_is_refused(tmp_path: Path) -> None:
    """Yesterday's leak was a shell write, so the shell seam is checked."""
    home = tmp_path / "home"
    cache = home / CACHE_REL
    root = _make_root(cache, "0.1.0", shell_honours=False)
    protected = tmp_path / "protected"
    protected.mkdir()

    proc = _run(PROBE, str(root), "--", "true", env=_env(home, protected))

    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "shell channel ignores the redirect" in proc.stderr
    assert "refusing to probe" in proc.stderr


def test_a_root_whose_python_ignores_the_redirect_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = home / CACHE_REL
    root = _make_root(cache, "0.1.0", python_honours=False)
    protected = tmp_path / "protected"
    protected.mkdir()

    proc = _run(PROBE, str(root), "--", "true", env=_env(home, protected))

    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "python channel ignores the redirect" in proc.stderr


def test_a_well_formed_root_passes_the_seam_and_runs_the_command(
    tmp_path: Path,
) -> None:
    """Positive control. Without it, a script that refused everything would pass
    both tests above and look like a working harness."""
    home = tmp_path / "home"
    cache = home / CACHE_REL
    root = _make_root(cache, "0.1.0")
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "url_index.db").write_text("real data")

    proc = _run(
        PROBE,
        str(root),
        "--",
        "bash",
        "-c",
        'printf x > "$ZOTERO_CAPTURE_STATE_DIR/capture.log"',
        env=_env(home, protected),
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "both channels honour the redirect" in proc.stdout
    assert "production state unchanged" in proc.stdout
    assert not (protected / "capture.log").exists(), "the probe wrote to production"


def test_a_write_to_production_state_is_a_loud_failure(tmp_path: Path) -> None:
    """The guard firing on a real leak, not on a synthetic one.

    A command that writes straight to the protected directory is exactly the
    2026-08-26 incident, and the run must fail rather than report a clean probe.
    """
    home = tmp_path / "home"
    cache = home / CACHE_REL
    root = _make_root(cache, "0.1.0")
    protected = tmp_path / "protected"
    protected.mkdir()

    proc = _run(
        PROBE,
        str(root),
        "--",
        "bash",
        "-c",
        f'printf leak >> "{protected}/capture.log"',
        env=_env(home, protected),
    )

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "PRODUCTION STATE CHANGED" in proc.stderr


# --- backport_trampoline.sh -------------------------------------------------


def test_a_root_without_a_trampoline_is_reported_and_left_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = home / CACHE_REL
    pinned = _make_root(cache, "0.23.0")
    stale = _make_root(cache, "0.20.2", trampolined=False)
    _write_registry(home, pinned)
    before = (stale / "hooks" / "run-python.sh").read_text()

    proc = _run(BACKPORT, env=_env(home, tmp_path / "protected"))

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "0.20.2/hooks/run-python.sh" in proc.stdout
    assert (stale / "hooks" / "run-python.sh").read_text() == before, "dry run wrote"


def test_apply_installs_the_pinned_copy_and_keeps_the_original(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = home / CACHE_REL
    pinned = _make_root(cache, "0.23.0")
    stale = _make_root(cache, "0.20.2", trampolined=False)
    _write_registry(home, pinned)
    original = (stale / "hooks" / "run-python.sh").read_text()

    proc = _run(BACKPORT, "--apply", env=_env(home, tmp_path / "protected"))

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    installed = (stale / "hooks" / "run-python.sh").read_text()
    assert installed == (pinned / "hooks" / "run-python.sh").read_text()
    assert os.access(stale / "hooks" / "run-python.sh", os.X_OK)

    backups = list(
        (home / ".local" / "state" / "zotero-provenance" / "backports").glob("*/*")
    )
    saved = [p for p in backups if p.name != "manifest.txt"]
    assert saved, (
        "the only copy of what a live process runs was overwritten with no backup"
    )
    assert any(p.read_text() == original for p in saved)


def test_verify_reports_forwarding_and_needs_the_pinned_root_to_decline(
    tmp_path: Path,
) -> None:
    """The sweep, with its own control: 'everything forwards' is only a result
    if something was capable of not forwarding."""
    home = tmp_path / "home"
    cache = home / CACHE_REL
    pinned = _make_root(cache, "0.23.0")
    _make_root(cache, "0.20.2")
    _write_registry(home, pinned)

    proc = _run(BACKPORT, "--verify", env=_env(home, tmp_path / "protected"))

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "0.20.2   forwards -> pinned" in proc.stdout
    assert "runs itself (pinned)" in proc.stdout
    assert "runs itself: yes" in proc.stdout


def test_verify_fails_when_a_root_does_not_forward(tmp_path: Path) -> None:
    """Seen to fail, deliberately: a root left on its own launcher."""
    home = tmp_path / "home"
    cache = home / CACHE_REL
    pinned = _make_root(cache, "0.23.0")
    _make_root(cache, "0.20.2", trampolined=False)
    _write_registry(home, pinned)

    proc = _run(BACKPORT, "--verify", env=_env(home, tmp_path / "protected"))

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "DOES NOT FORWARD" in proc.stdout
