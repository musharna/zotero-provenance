"""Refuse to write to the library from a plugin root that is out of date.

The capture hook does not run from this repository. It runs from a cache entry
keyed by version, and a session holds whichever entry it resolved at its own
start — so a long-lived session keeps executing an old release for as long as it
lives, while the repo, the clone and the tests all agree the bug is fixed.

That is not theoretical. On 2026-08-23 a session still on v0.3.0 wrote eight junk
URLs into the collection, "https://example.org/bar" among them, because v0.3.0
predates every exclusion rule: the reserved-name test that would have refused all
eight simply did not exist in the code that was running. Nothing anywhere
reported a problem. The index gained rows, the log recorded a successful capture,
and the only way to notice was to ask which version had produced them.

So an old root now declines to write and says why. Loud absence beats quiet
corruption — the same trade this plugin makes everywhere else.

The limit is worth stating plainly rather than discovering later: **a guard
cannot fix a version that predates it.** v0.3.0 will never refuse itself. This
closes the door from here forward; roots older than this release have to be
removed from disk instead, because there is no way to reason with them.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the plugin's installed copy lives. The cache entry the hook executes is
# built from this clone, so it is the authority on "what version is installed".
INSTALLED_MANIFEST = (
    Path.home()
    / ".claude"
    / "plugins"
    / "marketplaces"
    / "zotero-provenance"
    / ".claude-plugin"
    / "plugin.json"
)

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _parts(version: str) -> tuple[int, ...] | None:
    """A comparable tuple, or None when the string is not a plain version."""
    version = (version or "").strip()
    if not _VERSION_RE.match(version):
        return None
    return tuple(int(p) for p in version.split("."))


def is_stale(running: str, installed: str) -> bool:
    """True when `running` is strictly older than `installed`.

    Compared numerically, not lexicographically: "1.10.0" is newer than "1.2.3",
    and a string comparison gets that backwards.

    Unreadable input fails OPEN, which is the opposite of this plugin's usual
    rule and is deliberate. Everywhere else an unanswerable question means
    refuse; here a false positive silently stops capture for someone whose
    install layout we could not read, and that is worse than the occasional
    stale write it would have caught. The guard fires only when both versions
    parse and the comparison is unambiguous.
    """
    a, b = _parts(running), _parts(installed)
    if a is None or b is None:
        return False
    return a < b


def stale_reason(running: str, installed: str) -> str:
    """A log line naming both versions, or "" when the root is current.

    Both numbers are in the message on purpose: "your plugin is stale" is not
    actionable, while "running 0.3.0, 0.11.7 is installed" says exactly what
    happened and implies the fix, which is to start a new session.
    """
    if not is_stale(running, installed):
        return ""
    return (
        f"refusing to capture: this session is running plugin version {running}, "
        f"but {installed} is installed. A session keeps the plugin root it "
        f"resolved at its own start, so start a new session to pick it up. "
        f"Capturing from an old root writes rows that current rules would refuse."
    )


def installed_version(manifest: Path = INSTALLED_MANIFEST) -> str:
    """The version of the installed clone, or "" if it cannot be read."""
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError) as e:
        logger.debug("could not read installed version from %s: %s", manifest, e)
        return ""
