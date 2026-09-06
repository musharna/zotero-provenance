"""No file that ships may carry the maintainer's identity as fixture data.

A public repository is read by strangers. Test fixtures that name a real
hostname, a real tailnet address, a real Zotero library, or the maintainer's
private repositories are identity leaks: harmless as credentials, loud on
first view. The rule is mechanical so a new fixture cannot reintroduce one.
The only place the handle may appear is the plugin's own repository URL.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWN_REPO = "musharna/zotero-provenance"

# Anything in 100.64.0.0/10 is a CGNAT (tailnet) address, never a fixture.
# The network literal itself and its first host (100.64.0.1) are the only
# addresses in that range a fixture may name.
_CGNAT = re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!/\d)")
_CGNAT_ALLOWED = {"100.64.0.1"}
_HANDLE = re.compile(r"musharna(?!/zotero-provenance)")
_LIBRARY_ID = re.compile(r"\b6532713\b")


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "scripts", "tests", "hooks", "commands",
         "README.md", "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml", ".claude-plugin"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout
    files = [ROOT / p for p in out.decode().split("\0") if p]
    return [f for f in files if f.suffix != ".gz" and f.name != Path(__file__).name]


def _hits(pattern: re.Pattern[str], *, files=None) -> list[str]:
    found = []
    for f in files if files is not None else _tracked_text_files():
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            m = pattern.search(line)
            if m and m.group(0) not in _CGNAT_ALLOWED:
                found.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    return found


def test_the_guard_walks_the_tree():
    # Positive control: an empty file list would make every check below vacuous.
    files = _tracked_text_files()
    assert any(f.name == "snapshot.py" for f in files)
    assert any(f.name == "test_url_processing.py" for f in files)


def test_no_tailnet_address_is_used_as_a_fixture():
    assert _hits(_CGNAT) == []


def test_the_owner_handle_appears_only_in_the_plugin_repo_url():
    # Author metadata in the manifests is the handle by design; fixtures are not.
    shipped = [f for f in _tracked_text_files()
               if f.relative_to(ROOT).parts[0] in {"scripts", "tests", "hooks", "commands"}]
    assert _hits(_HANDLE, files=shipped) == []


def test_no_real_zotero_library_id_is_used_as_a_fixture():
    assert _hits(_LIBRARY_ID) == []


def test_no_owner_hostname_is_used_as_a_fixture():
    assert _hits(re.compile(r"mjarnold")) == []
