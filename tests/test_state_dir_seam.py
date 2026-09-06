"""The state directory is resolved from the ENVIRONMENT, everywhere.

`drain_queue.py` wrote `_state_dir({})`, which ignores ZOTERO_CAPTURE_STATE_DIR
and XDG_STATE_HOME and resolves to the production directory no matter what the
caller set: a drain run under an isolated state dir journalled its incidents
into the live health ledger, and the next SessionStart reported them. The
sixth stale second copy of one rule in this repository. `cli.py` had it right
one import away.

Derived, not named: every script under scripts/ is checked, so the next copy
cannot escape by being new.
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _state_dir_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "_state_dir")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "_state_dir")
        )
    ]


def _is_empty_env(call: ast.Call) -> bool:
    return bool(call.args) and isinstance(call.args[0], ast.Dict) and not call.args[0].keys


def test_no_script_resolves_the_state_dir_from_an_empty_env() -> None:
    # An AST walk, not a text scan: the first version of this test matched
    # the comment explaining the defect and reported the fix as the bug.
    offenders = sorted(
        str(p.relative_to(SCRIPTS))
        for p in SCRIPTS.rglob("*.py")
        if any(_is_empty_env(c) for c in _state_dir_calls(p))
    )
    assert offenders == [], offenders
    # Positive control: the seam is real and used -- a guard over a call that
    # nothing makes would pass forever.
    users = [p.name for p in SCRIPTS.rglob("*.py") if _state_dir_calls(p)]
    assert "drain_queue.py" in users and "cli.py" in users, users
