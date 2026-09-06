"""A counter nothing increments guards a warning nothing can reach.

`evaluate` declared `stale_n`, `stale_newest`, `stale_roots`, `unverified_n`
and `unverified_newest`, assigned them once to their zero values, and tested
them at the end. Nothing in between ever changed them, so the two warnings
they guarded were unreachable -- and a reader believed the log path still
reported unverified writes. Integrity lives in the ledger since 0.20.0.

The guard is a mechanism, not a name list: any local in `evaluate` assigned
exactly once, to a constant or an empty container, and never augmented or
reassigned, is a constant -- and a branch that tests a constant is dead.
"""

from __future__ import annotations

import ast
from pathlib import Path

from zotero_capture import health

HEALTH = Path(health.__file__)


def _constant_locals(fn: ast.FunctionDef) -> list[str]:
    stores: dict[str, list[ast.AST]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    stores.setdefault(t.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            stores.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            stores.setdefault(node.target.id, []).append(node)
        elif isinstance(node, (ast.For, ast.comprehension)):
            t = node.target
            if isinstance(t, ast.Name):
                stores.setdefault(t.id, []).append(node)

    def trivial(v: ast.AST | None) -> bool:
        if v is None:
            return False
        if isinstance(v, ast.Constant):
            return True
        if isinstance(v, (ast.List, ast.Set, ast.Dict, ast.Tuple)) and not getattr(v, "elts", None) and not getattr(v, "keys", None):
            return True
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in ("set", "list", "dict") and not v.args:
            return True
        return False

    # A name that is later mutated in place (`.add(`, `.append(`), or handed
    # to a callee that may fill it (`_each(lines, stats)`), is not constant.
    mutated = {
        n.func.value.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.attr in ("add", "append", "update", "extend", "setdefault")
    } | {
        a.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        for a in list(n.args) + [k.value for k in n.keywords]
        if isinstance(a, ast.Name)
    }
    return sorted(
        name
        for name, vals in stores.items()
        if len(vals) == 1 and trivial(vals[0]) and name not in mutated
    )


def test_evaluate_has_no_counter_that_nothing_increments() -> None:
    tree = ast.parse(HEALTH.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "evaluate")
    assert _constant_locals(fn) == []


def test_the_detector_sees_a_dead_counter() -> None:
    """Positive control: the guard must fail on the shape it exists for."""
    src = "def f(xs):\n    dead = 0\n    live = 0\n    for x in xs:\n        live += 1\n    if dead:\n        return 1\n    return live\n"
    fn = ast.parse(src).body[0]
    assert _constant_locals(fn) == ["dead"]
