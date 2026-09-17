"""The package is the unit that ships; the shims under scripts/ only point at it.

Three spellings must reach the same code: the hook shim (scripts/*.py, re-rooted
by the trampoline), `python -m zotero_capture`, and the console scripts of an
installed wheel. Before 0.64.0 the wheel had no entry points at all, so a
`pipx install` produced a package nobody could run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

CONSOLE_SCRIPTS = {
    "zotero-capture": "zotero_capture.cli:main",
    "zotero-capture-health": "zotero_capture.health_cli:main",
}


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
    return subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, env=env, timeout=60
    )


def test_python_m_zotero_capture_is_the_cli() -> None:
    r = _run("-m", "zotero_capture", "--help")
    assert r.returncode == 0, r.stderr
    assert "zotero-capture" in r.stdout  # argparse prog name from cli.build_parser


def test_python_m_health_cli_is_the_health_check() -> None:
    r = _run("-m", "zotero_capture.health_cli", "--help")
    assert r.returncode == 0, r.stderr
    assert "--ack" in r.stdout or "usage" in r.stdout.lower(), r.stdout


@pytest.mark.parametrize("name", sorted(CONSOLE_SCRIPTS))
def test_console_script_is_declared_and_resolves(name: str) -> None:
    """Reads the INSTALLED metadata, so it fails on a stale `pip install -e`
    just as it would on a wheel built without the [project.scripts] table."""
    eps = {ep.name: ep for ep in metadata.entry_points(group="console_scripts")}
    assert name in eps, f"{name} missing from console_scripts: {sorted(eps)}"
    assert eps[name].value == CONSOLE_SCRIPTS[name]
    assert callable(eps[name].load())


def test_the_pyproject_table_matches_this_test() -> None:
    """Positive control on the mapping above: a renamed target in pyproject
    must fail here rather than let the metadata test compare stale strings."""
    import configparser

    # No tomllib on 3.10 (still supported here). The [project.scripts] table is
    # flat `name = "module:func"` lines, which configparser reads verbatim.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    start = text.index("[project.scripts]")
    end = text.index("\n[", start + 1)
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text[start:end])
    table = {k: v.strip('"') for k, v in cp["project.scripts"].items()}
    assert table == CONSOLE_SCRIPTS


def test_the_hook_shims_are_shims() -> None:
    """The trampoline re-roots these two PATHS; the code must not live in them.
    A `def main` here would be a second copy the console script never sees."""
    import ast

    for shim in ("zotero_capture_main.py", "zotero_capture_health.py"):
        tree = ast.parse((SCRIPTS / shim).read_text(encoding="utf-8"))
        defs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert defs == [], f"{shim} defines {defs}; move them into the package"


def test_health_cli_knows_it_is_not_in_a_plugin_tree(tmp_path: Path) -> None:
    """Installed as a wheel there is no plugin.json above the module; the root
    must then be None (resolve_pinned: 'do not guess'), not scripts/."""
    from zotero_capture import health_cli

    assert health_cli._own_root() == ROOT  # from the tree: the plugin root
    fake = tmp_path / "site-packages" / "zotero_capture" / "health_cli.py"
    fake.parent.mkdir(parents=True)
    saved = health_cli.__file__
    try:
        health_cli.__file__ = str(fake)
        assert health_cli._own_root() is None
    finally:
        health_cli.__file__ = saved
