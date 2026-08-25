"""Which plugin root the manager actually pins, resolved exactly.

The first version of this asked for any key starting `zotero-provenance@` and
took the first match. A registry that legitimately holds the same plugin from
two marketplaces, or at two scopes, then resolved to whichever happened to be
serialised first — and the trampoline `exec`s whatever comes back. Nothing
adversarial is required: user, project and local scopes are ordinary, and
marketplace-qualified names are the documented identity.

So identity here is exact and derived from the caller's OWN path. A root living
at `.../cache/<marketplace>/<plugin>/<version>` may only ever resolve the key
`<plugin>@<marketplace>`, and only to a target inside that same subtree. When the
caller is not under the cache at all — a development checkout running the health
check — a single unambiguous entry is still usable, but two candidates refuse
rather than guess.

Ambiguity always resolves to None. Guessing is precisely what put an unverified
path on an `exec` line.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PLUGIN_NAME = "zotero-provenance"
_CACHE_PARTS = (".claude", "plugins", "cache")


def _identity(own_root: Path | None) -> tuple[str, str | None, Path | None]:
    """(plugin, marketplace, subtree) implied by where the caller lives.

    marketplace is None when the caller is not inside a plugin cache, which is
    the normal case for a checkout and means "cannot narrow by marketplace".
    """
    if own_root is None:
        return PLUGIN_NAME, None, None
    try:
        parts = own_root.resolve().parts
    except OSError:
        return PLUGIN_NAME, None, None
    for i in range(len(parts) - len(_CACHE_PARTS) - 2):
        if parts[i : i + len(_CACHE_PARTS)] == _CACHE_PARTS:
            rest = parts[i + len(_CACHE_PARTS) :]
            if len(rest) >= 3:
                marketplace, plugin = rest[0], rest[1]
                subtree = Path(*parts[: i + len(_CACHE_PARTS)]) / marketplace / plugin
                return plugin, marketplace, subtree
    return PLUGIN_NAME, None, None


def _parse_when(entry: dict) -> datetime | None:
    raw = entry.get("lastUpdated") or entry.get("installedAt")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def resolve_pinned(
    *, own_root: Path | None, registry_path: Path
) -> tuple[Path | None, datetime | None]:
    """The canonical pinned root and when it was pinned, or (None, None).

    None means "do not guess", never "nothing is installed" — callers must treat
    it as unknown rather than as a mismatch.
    """
    try:
        data = json.loads(Path(registry_path).read_text())
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None

    plugin, marketplace, subtree = _identity(own_root)
    wanted = f"{plugin}@{marketplace}" if marketplace else None

    # `{"plugins": [1]}` is valid JSON. Calling .items() on it raised, the health
    # checker caught the traceback and wrote it to a stderr the hook discards —
    # a crashed monitor that looked exactly like a healthy one.
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return None, None

    candidates: list[tuple[Path, datetime | None]] = []
    for key, entries in plugins.items():
        if not isinstance(key, str) or not isinstance(entries, list):
            continue
        if wanted is not None:
            if key != wanted:
                continue
        elif key.rsplit("@", 1)[0] != plugin or "@" not in key:
            continue
        for entry in entries:
            # Every entry must be well-formed. Skipping malformed ones let this
            # resolver accept a registry the shell trampolines refuse — two
            # implementations claiming one policy and quietly disagreeing, which
            # is how the original first-prefix-match bug survived so long.
            if not isinstance(entry, dict):
                return None, None
            raw = entry.get("installPath")
            if not isinstance(raw, str) or not raw:
                return None, None
            try:
                path = Path(raw).resolve()
            except OSError:
                continue
            candidates.append((path, _parse_when(entry)))

    distinct = {path for path, _ in candidates}
    if len(distinct) != 1:
        return None, None

    resolved = distinct.pop()
    # A root may only ever forward inside its own marketplace/plugin subtree.
    if subtree is not None:
        try:
            resolved.relative_to(subtree.resolve())
        except ValueError:
            return None, None
    # The NEWEST timestamp among entries naming this root, not whichever was
    # serialised first — two entries agreeing on the path but differing in
    # lastUpdated used to return an arbitrary one, which moved health's scope
    # depending on key order.
    stamps = [w for path, w in candidates if path == resolved and w is not None]
    return resolved, (max(stamps) if stamps else None)
