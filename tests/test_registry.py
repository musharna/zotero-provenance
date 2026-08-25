"""Resolving which plugin root the manager actually pins.

The first implementation asked for any key starting `zotero-provenance@` and took
the first hit. A registry that legitimately holds the same plugin from two
marketplaces, or at two scopes, then resolves to whichever happened to be
serialised first — and the trampoline `exec`s it. That is not an attack; user,
project and local scopes and marketplace-qualified names are ordinary.

So identity is exact and derived from the caller's OWN path: a root at
`.../cache/<marketplace>/<plugin>/<version>` may only ever resolve
`<plugin>@<marketplace>`, and only to a target inside that same subtree.
Ambiguity refuses instead of guessing, because guessing is what put the wrong
path on an `exec` line.
"""

from __future__ import annotations

import json
from pathlib import Path

from zotero_capture.registry import resolve_pinned

PLUGIN = "zotero-provenance"
MARKET = "zotero-provenance"


def _cache(home: Path) -> Path:
    return home / ".claude" / "plugins" / "cache"


def _root(
    home: Path, version: str, *, market: str = MARKET, plugin: str = PLUGIN
) -> Path:
    path = _cache(home) / market / plugin / version
    (path / "hooks").mkdir(parents=True, exist_ok=True)
    return path


def _registry(home: Path, entries: dict) -> Path:
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": entries}))
    return path


def test_a_different_marketplace_is_not_followed(tmp_path: Path) -> None:
    """The audit's reproduction: another marketplace serialised first won."""
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    correct = _root(home, "0.15.0")
    other = _root(home, "1.0.0", market="other-marketplace")
    reg = _registry(
        home,
        {
            f"{PLUGIN}@other-marketplace": [
                {"scope": "project", "installPath": str(other)}
            ],
            f"{PLUGIN}@{MARKET}": [{"scope": "user", "installPath": str(correct)}],
        },
    )

    resolved, _ = resolve_pinned(own_root=mine, registry_path=reg)

    assert resolved == correct.resolve()


def test_two_scopes_disagreeing_refuses_rather_than_picks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    a, b = _root(home, "0.15.0"), _root(home, "0.14.1")
    reg = _registry(
        home,
        {
            f"{PLUGIN}@{MARKET}": [
                {"scope": "user", "installPath": str(a)},
                {"scope": "project", "installPath": str(b)},
            ]
        },
    )

    resolved, _ = resolve_pinned(own_root=mine, registry_path=reg)

    assert resolved is None


def test_two_scopes_agreeing_is_not_ambiguous(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    same = _root(home, "0.15.0")
    reg = _registry(
        home,
        {
            f"{PLUGIN}@{MARKET}": [
                {"scope": "user", "installPath": str(same)},
                {"scope": "project", "installPath": str(same) + "/"},
            ]
        },
    )

    resolved, _ = resolve_pinned(own_root=mine, registry_path=reg)

    assert resolved == same.resolve()


def test_a_trailing_slash_is_the_same_root(tmp_path: Path) -> None:
    """Otherwise a root forwards to itself forever and every capture is lost."""
    home = tmp_path / "home"
    mine = _root(home, "0.15.0")
    reg = _registry(
        home,
        {f"{PLUGIN}@{MARKET}": [{"scope": "user", "installPath": str(mine) + "/"}]},
    )

    resolved, _ = resolve_pinned(own_root=mine, registry_path=reg)

    assert resolved == mine.resolve()


def test_a_target_outside_the_cache_subtree_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    outside = tmp_path / "elsewhere"
    (outside / "hooks").mkdir(parents=True)
    reg = _registry(
        home, {f"{PLUGIN}@{MARKET}": [{"scope": "user", "installPath": str(outside)}]}
    )

    resolved, _ = resolve_pinned(own_root=mine, registry_path=reg)

    assert resolved is None


def test_a_missing_entry_resolves_to_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    reg = _registry(home, {"something-else@elsewhere": [{"installPath": "/x"}]})

    resolved, _ = resolve_pinned(own_root=mine, registry_path=reg)

    assert resolved is None


def test_an_unreadable_registry_resolves_to_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")

    resolved, when = resolve_pinned(own_root=mine, registry_path=home / "absent.json")

    assert resolved is None and when is None


def test_the_install_timestamp_comes_back_with_the_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    target = _root(home, "0.15.0")
    reg = _registry(
        home,
        {
            f"{PLUGIN}@{MARKET}": [
                {
                    "scope": "user",
                    "installPath": str(target),
                    "lastUpdated": "2026-08-25T09:40:01.181Z",
                }
            ]
        },
    )

    resolved, when = resolve_pinned(own_root=mine, registry_path=reg)

    assert resolved == target.resolve()
    assert when is not None and when.year == 2026 and when.tzinfo is not None


def test_a_checkout_outside_the_cache_still_resolves_a_single_entry(
    tmp_path: Path,
) -> None:
    """Health runs from a checkout too; one unambiguous entry is still usable."""
    home = tmp_path / "home"
    target = _root(home, "0.15.0")
    reg = _registry(
        home, {f"{PLUGIN}@{MARKET}": [{"scope": "user", "installPath": str(target)}]}
    )

    resolved, _ = resolve_pinned(own_root=tmp_path / "checkout", registry_path=reg)

    assert resolved == target.resolve()


def test_a_checkout_refuses_when_marketplaces_disagree(tmp_path: Path) -> None:
    """With no own-path to disambiguate, two candidates must not be guessed between."""
    home = tmp_path / "home"
    a = _root(home, "0.15.0")
    b = _root(home, "1.0.0", market="other-marketplace")
    reg = _registry(
        home,
        {
            f"{PLUGIN}@{MARKET}": [{"scope": "user", "installPath": str(a)}],
            f"{PLUGIN}@other-marketplace": [{"scope": "user", "installPath": str(b)}],
        },
    )

    resolved, _ = resolve_pinned(own_root=tmp_path / "checkout", registry_path=reg)

    assert resolved is None


def test_a_malformed_plugins_value_does_not_crash(tmp_path: Path) -> None:
    """`{"plugins": [1]}` is valid JSON. It used to raise AttributeError, which
    the health checker caught and printed to a stderr the hook discards —
    silence indistinguishable from health."""
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"plugins": [1]}))

    assert resolve_pinned(own_root=mine, registry_path=reg) == (None, None)


def test_entries_that_are_not_a_list_are_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"plugins": {f"{PLUGIN}@{MARKET}": "not-a-list"}}))

    assert resolve_pinned(own_root=mine, registry_path=reg) == (None, None)


def test_a_malformed_entry_refuses_like_the_shell_does(tmp_path: Path) -> None:
    """The shell trampolines refuse `{}`; Python accepted the valid sibling.

    Not an exec path — managed hooks go through the shell — but two resolvers
    claiming one policy and disagreeing is how the first-prefix-match bug
    survived as long as it did.
    """
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    good = _root(home, "1.0.0")
    reg = _registry(
        home,
        {f"{PLUGIN}@{MARKET}": [{"scope": "user", "installPath": str(good)}, {}]},
    )

    assert resolve_pinned(own_root=mine, registry_path=reg) == (None, None)


def test_a_non_string_install_path_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = _root(home, "0.9.0")
    good = _root(home, "1.0.0")
    reg = _registry(
        home,
        {f"{PLUGIN}@{MARKET}": [
            {"scope": "user", "installPath": str(good)},
            {"scope": "project", "installPath": 7},
        ]},
    )

    assert resolve_pinned(own_root=mine, registry_path=reg) == (None, None)
