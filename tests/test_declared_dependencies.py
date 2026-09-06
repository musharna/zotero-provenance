"""Every third-party import in the plugin is declared in pyproject.toml.

`url_processing.py` imported `idna` on the hot path and pyproject listed four
packages without it; the README's manual pip line had five. Anyone installing
from the manifest got an import crash that the hook reported as "capture is
off". Derived from the imports, not from a list of packages someone remembers.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# import name -> distribution name, where they differ.
DIST = {"bs4": "beautifulsoup4", "markdown_it": "markdown-it-py", "linkify_it": "linkify-it-py"}


def _top_level_imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def test_every_third_party_import_is_a_declared_dependency() -> None:
    # No tomllib: it arrived in 3.11 and the manifest promises 3.10. CI's
    # 3.10 jobs were the first thing to catch that, in this very test.
    block = re.search(r"^dependencies = \[(.*?)^\]", (ROOT / "pyproject.toml").read_text(), re.S | re.M)
    assert block, "pyproject.toml has no dependencies list"
    declared = {
        m.group(1).lower()
        for m in re.finditer(r'^\s*"([A-Za-z0-9_.-]+)', block.group(1), re.M)
    }
    local = {p.stem for p in SCRIPTS.glob("*.py")} | {p.name for p in SCRIPTS.iterdir() if p.is_dir()}
    stdlib = set(sys.stdlib_module_names)
    third_party: set[str] = set()
    for path in SCRIPTS.rglob("*.py"):
        third_party |= _top_level_imports(path) - stdlib - local - {"zotero_capture"}
    missing = sorted(m for m in third_party if DIST.get(m, m).replace("_", "-").lower() not in declared)
    assert missing == [], f"imported but not declared in pyproject: {missing}"
    assert "httpx" in third_party  # positive control: the scan saw a real import
