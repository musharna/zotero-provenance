"""Where "installed" comes from: the pinned root, not the clone it was built from.

On 2026-09-01 four captures were refused by a root that was, in fact, exactly
the root the registry pinned. The shell trampoline asked
`installed_plugins.json` which root to run and answered "this one, run
yourself"; the guard then asked the marketplace clone's manifest what was
installed and answered "a different version, refuse". Two files holding one
fact, disagreeing for the eleven hours that separated their mtimes.

Nothing in this suite could have caught it, because nothing here exercised it:
`conftest._installed_version_matches` stubs `installed_version` out wholesale
for `capture` and `cli`, so the COMPARISON was covered from the first release
and the RESOLUTION -- which file is read -- never was. A stub that makes the
rest of the suite machine-independent also made this defect unreachable, which
is the same shape as the frozen `now="NOW"` clock and the `init_db` every
fixture called.

These tests address `staleness` directly, which the autouse fixture does not
touch, so the resolution is what is under test.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import zotero_capture.registry as reg
import zotero_capture.staleness as st

MARKET = "zotero-provenance"


def _plugin_root(base: Path, version: str) -> Path:
    """A cache entry laid out the way the plugin manager lays one out."""
    root = base / ".claude" / "plugins" / "cache" / MARKET / MARKET / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": MARKET, "version": version}), encoding="utf-8"
    )
    return root


def _registry(base: Path, *pinned: Path) -> Path:
    path = base / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "plugins": {
                    f"{MARKET}@{MARKET}": [
                        {
                            "installPath": str(p),
                            "lastUpdated": "2026-09-02T00:00:00+00:00",
                        }
                        for p in pinned
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _disagreeing_clone(base: Path, version: str) -> Path:
    """A marketplace clone that says something else.

    Written on purpose in every test below even though nothing should read it.
    It is the trap: if a future change goes back to consulting the clone, these
    tests do not merely stop covering the case, they go red.
    """
    path = (
        base
        / ".claude"
        / "plugins"
        / "marketplaces"
        / MARKET
        / ".claude-plugin"
        / "plugin.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version}), encoding="utf-8")
    return path


def _point_at(monkeypatch, registry: Path, own_root: Path) -> None:
    monkeypatch.delenv("ZOTERO_PROVENANCE_INSTALLED_MANIFEST", raising=False)
    # The registry path lives in exactly one place, so this is the one name
    # to patch. If a module ever grows its own copy again, this stops
    # reaching it and these tests read the developer's real registry.
    monkeypatch.setattr(reg, "REGISTRY_PATH", registry)
    monkeypatch.setattr(st, "OWN_ROOT", own_root)


def test_installed_is_the_pinned_root_not_the_clone(tmp_path, monkeypatch) -> None:
    """The discriminator: the two sources are made to disagree.

    A test where they agree cannot tell which one was read, which is precisely
    why eleven hours of drift went unnoticed.
    """
    pinned = _plugin_root(tmp_path, "0.9.9")
    _disagreeing_clone(tmp_path, "0.1.1")
    _point_at(monkeypatch, _registry(tmp_path, pinned), pinned)

    assert st.installed_version() == "0.9.9"


def test_a_clone_ahead_of_the_install_is_not_staleness(tmp_path, monkeypatch) -> None:
    """The 2026-09-01 refusals, reproduced.

    Running 0.44.0 from the root the registry pins, while the clone has already
    moved to 0.45.0. A new session would resolve and run this same root, so
    this session is running exactly the code the install specifies. Pulling an
    update that has not been installed is not a stale session, and refusing
    here takes capture down for every live session in exchange for nothing --
    the 29-hour outage of 2026-08-24, re-run.
    """
    pinned = _plugin_root(tmp_path, "0.44.0")
    _disagreeing_clone(tmp_path, "0.45.0")
    _point_at(monkeypatch, _registry(tmp_path, pinned), pinned)

    assert st.installed_version() == "0.44.0"
    assert st.stale_reason("0.44.0", st.installed_version()) == ""


def test_a_superseded_root_still_refuses(tmp_path, monkeypatch) -> None:
    """The positive control for the guard itself.

    Everything above narrows when the guard fires. If that narrowing had gone
    one step too far the guard would be off, and every test above would still
    pass -- "does not refuse" is satisfied by a guard that never refuses.
    """
    pinned = _plugin_root(tmp_path, "0.48.0")
    _disagreeing_clone(tmp_path, "0.48.0")
    _point_at(monkeypatch, _registry(tmp_path, pinned), pinned)

    reason = st.stale_reason("0.3.0", st.installed_version())
    assert "refusing to capture" in reason
    assert "0.3.0" in reason and "0.48.0" in reason


def test_unresolvable_is_unknown_and_resolvable_still_refuses(
    tmp_path, monkeypatch
) -> None:
    """Both directions, in one test, because either alone is satisfiable wrongly.

    `registry.resolve_pinned` documents None as "do not guess", never "nothing
    is installed". Turning an unreadable registry into a refusal would switch
    capture off for anyone whose layout this code cannot read -- worse than the
    stale write it prevents, and invisible the same way.
    """
    _disagreeing_clone(tmp_path, "9.9.9")
    _point_at(monkeypatch, tmp_path / "absent.json", tmp_path / "checkout")

    assert st.installed_version() == ""
    assert st.stale_reason("0.1.0", st.installed_version()) == ""

    pinned = _plugin_root(tmp_path, "0.2.0")
    _point_at(monkeypatch, _registry(tmp_path, pinned), pinned)

    assert st.installed_version() == "0.2.0"
    assert "refusing to capture" in st.stale_reason("0.1.0", st.installed_version())


def test_an_ambiguous_registry_is_unknown(tmp_path, monkeypatch) -> None:
    """Two candidates refuse to resolve, and that must not read as a mismatch."""
    a = _plugin_root(tmp_path, "0.1.0")
    b = _plugin_root(tmp_path, "0.2.0")
    _disagreeing_clone(tmp_path, "0.3.0")
    _point_at(monkeypatch, _registry(tmp_path, a, b), a)

    assert st.installed_version() == ""
    assert st.stale_reason("0.1.0", st.installed_version()) == ""


def test_a_pinned_root_with_no_readable_manifest_is_unknown(
    tmp_path, monkeypatch
) -> None:
    """The registry can name a root whose manifest is gone or corrupt."""
    pinned = _plugin_root(tmp_path, "0.5.0")
    (pinned / ".claude-plugin" / "plugin.json").write_text(
        "{not json", encoding="utf-8"
    )
    _disagreeing_clone(tmp_path, "0.6.0")
    _point_at(monkeypatch, _registry(tmp_path, pinned), pinned)

    assert st.installed_version() == ""
    assert st.stale_reason("0.5.0", st.installed_version()) == ""


def test_the_env_override_still_wins(tmp_path, monkeypatch) -> None:
    """The one documented seam, and the only one: it is what the e2e tests use."""
    pinned = _plugin_root(tmp_path, "0.9.9")
    _point_at(monkeypatch, _registry(tmp_path, pinned), pinned)
    override = tmp_path / "elsewhere.json"
    override.write_text(json.dumps({"version": "7.7.7"}), encoding="utf-8")
    monkeypatch.setenv("ZOTERO_PROVENANCE_INSTALLED_MANIFEST", str(override))

    assert st.installed_version() == "7.7.7"


def test_there_is_one_way_to_ask(tmp_path) -> None:
    """No parameter, so no third answer can be threaded in.

    `installed_version(manifest=...)` was never passed by anything: both
    production call sites call it bare and no test used it, while its docstring
    claimed "two callers need that". The end-to-end tests use the environment
    variable. A parameter nothing passes is the `--sleep` defect -- a way in
    that looks supported and is not exercised.
    """
    assert list(inspect.signature(st.installed_version).parameters) == []


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def _literals_matching(needle: str) -> list[str]:
    """Every non-docstring string constant containing `needle`, package-wide.

    Walks the whole `scripts/` tree rather than a list of modules. A guard
    that names its subjects cannot fail on a module that does not exist yet,
    which is the same defect in a test that the test exists to prevent in the
    code -- the User-Agent guard named its call sites and so could not see a
    fourth copy appear.

    Prose is skipped: docstrings here discuss both of these by name, and a
    sentence is not a path.
    """
    scripts = Path(st.__file__).resolve().parents[1]
    found: list[str] = []
    for path in sorted(scripts.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and needle in node.value
                and id(node) not in skip
            ):
                found.append(f"{path.name}:{node.lineno}")
    return found


def test_only_the_registry_module_names_the_registry_file() -> None:
    """One fact, one holder -- checked one level below the version itself.

    staleness, the capture record and the health check each built this path
    from scratch. They had not drifted, which is exactly why it was worth
    collapsing now rather than after the fifth copy appeared.
    """
    found = _literals_matching("installed_plugins.json")
    # Both directions. "no module outside registry.py names it" is satisfied by
    # a codebase that names it nowhere at all -- which would mean the constant
    # had been renamed and this guard had quietly stopped guarding anything.
    assert found, "nothing names the registry file; this guard is now vacuous"
    assert {f.split(":")[0] for f in found} == {"registry.py"}, (
        f"the registry path is written outside registry.py: {found}"
    )


def test_no_module_builds_a_path_into_the_marketplace_clone() -> None:
    """Derived over the whole package, never a list of modules.

    A guard naming staleness.py cannot fail on a module that does not exist
    yet, which is the same defect in a test that the test exists to prevent in
    the code -- the User-Agent guard named its subjects and so could not see a
    fourth copy appear. This reads every module in the package.

    Prose is skipped: registry.py's docstring discusses "two marketplaces" and
    that is a sentence, not a path.
    """
    offenders = _literals_matching("marketplaces")
    assert offenders == [], (
        "a module resolves the install from the marketplace clone rather than "
        f"from the root the registry pins: {offenders}"
    )
