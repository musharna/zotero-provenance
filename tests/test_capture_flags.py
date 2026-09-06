"""Every flag the capture CLI parses is read.

`--session` and `--message-from-stdin` were parsed and never read: both hooks
passed both on every fire, the session id was dropped on the floor (no log
line could be joined to a session), and stdin was read whenever `--message`
was absent regardless of the flag. The dead-flag AST guards covered
snapshot_pages.py only -- a guard that names its subject checks the set of
files alive the day it was written.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import zotero_capture.cli as cli

CLI = Path(cli.__file__)


def _flags_and_reads(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text())
    flags = {
        str(a.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_argument"
        for a in n.args
        if isinstance(a, ast.Constant) and str(a.value).startswith("--")
    }
    reads = {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "args"
    }
    return flags, reads


def test_every_capture_flag_is_read() -> None:
    flags, reads = _flags_and_reads(CLI)
    assert "--message" in flags  # positive control: the parser was found
    dead = sorted(f for f in flags if f.lstrip("-").replace("-", "_") not in reads)
    assert dead == [], dead


def _configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_WEBSOURCES_COLLECTION_KEY", "C")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.delenv("ZOTERO_CAPTURE_DISABLE", raising=False)


def test_the_session_id_reaches_the_capture_record(tmp_path, monkeypatch) -> None:
    _configured(tmp_path, monkeypatch)
    seen: dict = {}

    def fake_run_capture(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "run_capture", fake_run_capture)
    cli.main(["--cwd", "/tmp", "--session", "sess-1", "--message", "see https://x.test/a"])
    assert seen.get("session") == "sess-1", sorted(seen)


def test_message_from_stdin_is_honoured_over_an_inline_message(tmp_path, monkeypatch) -> None:
    """With both given, the flag names the source. Before, `--message` won
    silently and the flag meant nothing."""
    import io

    _configured(tmp_path, monkeypatch)
    seen: dict = {}

    def fake_run_capture(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "run_capture", fake_run_capture)
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin https://y.test/b"))
    cli.main(["--cwd", "/tmp", "--message-from-stdin", "--message", "inline"])
    assert seen.get("message_from_stdin") is True, sorted(seen)
