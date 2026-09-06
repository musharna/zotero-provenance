"""Rules this repository has kept in more than one place, pinned to one.

Stale second copies are the defect class shipped most often here (six times
by 0.60.0), against zero missing guards. Each test below either derives the
rule's every reader from one definition, or -- where a shell copy is
deliberate -- asserts the copies agree so drift is a failing test rather than
a quiet divergence.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from zotero_capture import url_processing
from zotero_capture.config import _state_dir

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"


# --- L5: which hosts are DOI hosts --------------------------------------------


def test_a_malformed_doi_on_www_doi_org_is_excluded_too() -> None:
    """`www.doi.org` was a DOI host to the title resolver and the DOI gate,
    and not to the exclusion rule: a malformed `www.doi.org/10.x` was
    captured, then resolved. Three lists, one of them shorter."""
    assert url_processing.is_excluded("https://www.doi.org/10.x")
    # Positive controls: the rule still fires on the hosts it knew, and a
    # well-formed DOI is not excluded on any of them.
    assert url_processing.is_excluded("https://doi.org/10.x")
    assert not url_processing.is_excluded("https://www.doi.org/10.1234/abc")


def test_every_doi_host_reader_derives_from_one_definition() -> None:
    from zotero_capture import doi_gate, title_fetcher

    hosts = url_processing.DOI_HOSTS
    assert {"doi.org", "dx.doi.org", "www.doi.org"} <= hosts
    for h in hosts:
        assert title_fetcher._RESOLVERS[h] is title_fetcher._doi_title, h
        assert doi_gate._DOI_URL_RE.match(f"https://{h}/10.1234/abc"), h


# --- L5: the state-dir rule, kept in shell on purpose --------------------------


@pytest.mark.parametrize(
    "env",
    [
        {"ZOTERO_CAPTURE_STATE_DIR": "/explicit/state"},
        {"XDG_STATE_HOME": "/xdg/state"},
        {},
    ],
    ids=["explicit", "xdg", "default"],
)
def test_the_shell_state_dir_matches_the_python_one(tmp_path: Path, env: dict) -> None:
    """The trampoline resolves the state dir inline, before Python exists,
    and lib.sh does it again. Both are documented as deliberate. This is the
    parity guard: it passes today, and was mutated by hand (an `s` dropped
    from `.local/state` in lib.sh) to see it fail before it shipped."""
    full = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], **env}
    shell = subprocess.run(
        ["bash", "-c", f'source "{HOOKS}/lib.sh"; zp_log_path'],
        env=full, capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    assert shell == str(_state_dir(full) / "capture.log")


# --- L6: a hook's JSON line is JSON whatever the path contains -----------------


def test_a_quote_in_the_root_path_still_yields_a_json_line(tmp_path: Path) -> None:
    """`zp_tramp_log` interpolated `$ZP_MINE` and the detail into JSON with
    printf, unescaped. A path holding `"` produced a line health counted as
    an unreadable log -- reporting a fault about the writer instead of the
    event the writer was recording."""
    from test_trampoline import CACHE_REL, _env, _fake_python, _payload, _run

    home = tmp_path / "home"
    mine = home / CACHE_REL / 'v"0.9.0'
    (mine / "hooks").mkdir(parents=True)
    for name in ("capture-stop.sh", "lib.sh", "run-python.sh"):
        (mine / "hooks" / name).write_bytes((HOOKS / name).read_bytes())
    (mine / "scripts").mkdir()
    _fake_python(tmp_path)
    # No registry: the root cannot resolve a target and logs forward-unresolved.
    proc = _run(mine / "hooks" / "capture-stop.sh", _env(tmp_path, home), _payload())
    assert proc.returncode == 0, proc.stderr

    log = tmp_path / "state" / "capture.log"
    lines = log.read_text().splitlines()
    assert lines, "nothing was logged"
    for line in lines:
        rec = json.loads(line)  # the assertion: every line parses
        assert rec["event"] == "forward-unresolved"
        assert '"' in rec["self"], "the escaped quote must survive the round trip"
