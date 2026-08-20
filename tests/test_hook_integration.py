"""Real-execution tests: run the actual hook scripts as subprocesses.

The unit tests exercise Python in isolation; these drive the shell entry points
the way Claude Code drives them, which is the only place bugs in jq parsing,
argument threading, and the never-block guarantee can show up.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT

STOP_HOOK = PLUGIN_ROOT / "hooks" / "capture-stop.sh"
PROMPT_HOOK = PLUGIN_ROOT / "hooks" / "capture-prompt.sh"
ENTRY = PLUGIN_ROOT / "scripts" / "zotero_capture_main.py"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _clean_env(tmp_path: Path) -> dict[str, str]:
    """Env with no credentials, no disable flag, and a hermetic state dir."""
    env = os.environ.copy()
    for var in (
        "ZOTERO_CAPTURE_DISABLE",
        "ZOTERO_API_KEY",
        "ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_TYPE",
        "ZOTERO_WEBSOURCES_COLLECTION_KEY",
    ):
        env.pop(var, None)
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(tmp_path / "state")
    # Point at a secrets file that does not exist, so a real one on this machine
    # can never leak into the test.
    env["ZOTERO_SECRETS_FILE"] = str(tmp_path / "no-such-secrets.env")
    return env


def _transcript(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _assistant_line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _fake_python(tmp_path: Path) -> Path:
    """A stub `python3` that records argv instead of running the real entry point."""
    argv_out = tmp_path / "argv.txt"
    stub = tmp_path / "python3"
    stub.write_text(f'#!/bin/sh\necho "$@" > {argv_out}\ncat > /dev/null\n')
    stub.chmod(0o755)
    return argv_out


# --- the never-block guarantee ------------------------------------------------


def test_entry_exits_zero_when_disabled(tmp_path: Path) -> None:
    db = tmp_path / "capture.db"
    env = _clean_env(tmp_path)
    env["ZOTERO_CAPTURE_DISABLE"] = "1"
    proc = subprocess.run(
        ["python3", str(ENTRY), "--message", "see https://example.com/foo",
         "--db-path", str(db)],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert not db.exists(), "no database may be created while capture is disabled"


def test_entry_exits_zero_without_credentials(tmp_path: Path) -> None:
    """A misconfigured plugin must not break the user's session."""
    proc = subprocess.run(
        ["python3", str(ENTRY), "--message", "see https://example.com/foo",
         "--db-path", str(tmp_path / "capture.db")],
        env=_clean_env(tmp_path), capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, (
        f"never-block guarantee violated: exit {proc.returncode}\n{proc.stderr}"
    )


def test_missing_credentials_are_reported_not_swallowed(tmp_path: Path) -> None:
    """Exiting 0 must not mean staying silent — the reason belongs on stderr."""
    proc = subprocess.run(
        ["python3", str(ENTRY), "--message", "see https://example.com/foo",
         "--db-path", str(tmp_path / "capture.db")],
        env=_clean_env(tmp_path), capture_output=True, text=True, timeout=20,
    )
    assert "ZOTERO_API_KEY" in proc.stderr, (
        f"a silent failure is a debugging dead end; stderr was: {proc.stderr!r}"
    )


# --- the Stop hook ------------------------------------------------------------


@requires_jq
def test_stop_hook_passes_cwd_through_to_python(tmp_path: Path) -> None:
    argv_out = _fake_python(tmp_path)
    transcript = _transcript(tmp_path, _assistant_line("see https://example.com/foo"))
    env = _clean_env(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=json.dumps(
            {"transcript_path": str(transcript), "session_id": "s1",
             "cwd": "/home/someone/agrigen"}
        ),
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert argv_out.exists(), f"python was never invoked; stderr: {proc.stderr!r}"
    assert "--cwd /home/someone/agrigen" in argv_out.read_text()


@requires_jq
def test_stop_hook_survives_a_malformed_transcript_line(tmp_path: Path) -> None:
    """One corrupt line must not strand the URLs on every line after it.

    A plain `jq` pass aborts the whole file on the first parse error; the hook
    uses a per-line tolerant parse specifically to prevent that.
    """
    argv_out = _fake_python(tmp_path)
    transcript = _transcript(
        tmp_path,
        "{this is not json at all",
        _assistant_line("see https://example.com/after-the-bad-line"),
    )
    env = _clean_env(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=json.dumps(
            {"transcript_path": str(transcript), "session_id": "s1", "cwd": "/tmp/x"}
        ),
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert argv_out.exists(), (
        "the malformed line aborted extraction, stranding every later URL"
    )


@requires_jq
def test_stop_hook_reads_the_context_marker(tmp_path: Path) -> None:
    argv_out = _fake_python(tmp_path)
    transcript = _transcript(
        tmp_path,
        _assistant_line("[SOURCE-CONTEXT: lit-review] see https://example.com/foo"),
    )
    env = _clean_env(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"

    subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=json.dumps(
            {"transcript_path": str(transcript), "session_id": "s1", "cwd": "/tmp/x"}
        ),
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert "--context lit-review" in argv_out.read_text()


@requires_jq
def test_stop_hook_skips_a_message_with_no_urls(tmp_path: Path) -> None:
    """Positive control for the two tests above: the stub runs when there IS a URL,
    so its absence here means the pre-filter worked, not that the harness broke."""
    argv_out = _fake_python(tmp_path)
    transcript = _transcript(tmp_path, _assistant_line("no links in this message"))
    env = _clean_env(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=json.dumps(
            {"transcript_path": str(transcript), "session_id": "s1", "cwd": "/tmp/x"}
        ),
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0
    assert not argv_out.exists(), "python should not be invoked for a URL-free message"


# --- the UserPromptSubmit hook ------------------------------------------------


@requires_jq
def test_prompt_hook_never_writes_to_stdout(tmp_path: Path) -> None:
    """UserPromptSubmit stdout is injected into the user's prompt — it must stay empty."""
    _fake_python(tmp_path)
    env = _clean_env(tmp_path)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(PROMPT_HOOK)],
        input=json.dumps(
            {"prompt": "look at https://example.com/foo", "session_id": "s1",
             "cwd": "/home/someone/agrigen"}
        ),
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0
    assert proc.stdout == "", (
        f"stdout would be injected into the prompt; got {proc.stdout!r}"
    )
