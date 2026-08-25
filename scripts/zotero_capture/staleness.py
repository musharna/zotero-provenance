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
import os
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


CURRENT = "current"
MISMATCH = "mismatch"
UNKNOWN = "unknown"


def classify(running: str, installed: str) -> str:
    """CURRENT, MISMATCH or UNKNOWN — three answers, because there are three.

    This was a boolean, and both of its shortcuts were real holes.

    It asked "is running OLDER", so a root NEWER than the install was called
    fine. But rolling the install back to 0.11.6 is precisely how you stop a bad
    0.11.7 from writing, and under that rule the rollback did nothing to the
    session it was meant to stop. Agreement now has to be exact.

    And an unparseable version returned False — the same value as "verified
    current" — so an install layout the code could not read silently disabled
    the only guard against writing from an unknown root. UNKNOWN is now its own
    answer, and the caller decides what it is worth.

    Comparison is on parsed integers, not strings: "1.10.0" is newer than
    "1.2.3" and a lexicographic compare gets that backwards.
    """
    a, b = _parts(running), _parts(installed)
    if a is None or b is None:
        return UNKNOWN
    return CURRENT if a == b else MISMATCH


def is_stale(running: str, installed: str) -> bool:
    """Whether capture must refuse. UNKNOWN does not refuse; see stale_reason."""
    return classify(running, installed) is MISMATCH


def stale_reason(running: str, installed: str) -> str:
    """A log line naming both versions, or "" when the root is current.

    Both numbers are in the message on purpose: "your plugin is stale" is not
    actionable, while "running 0.3.0, 0.11.7 is installed" says exactly what
    happened and implies the fix, which is to start a new session.
    """
    verdict = classify(running, installed)
    if verdict is CURRENT:
        return ""
    if verdict is UNKNOWN:
        # Deliberately NOT a refusal, and deliberately not silent either.
        #
        # Refusing here would switch capture off for anyone whose install
        # layout this code cannot parse — a failure that is both worse than the
        # stale write it prevents and invisible in exactly the same way, which
        # is the property that made the v0.3.0 outage last weeks. So capture
        # proceeds and the uncertainty is logged. This is the one place the
        # plugin's refuse-on-doubt rule is inverted, and the inversion is the
        # whole reason it is written down here.
        logger.warning(
            "cannot compare plugin versions (running %r, installed %r); "
            "capturing anyway",
            running,
            installed,
        )
        return ""
    return (
        f"refusing to capture: this session is running plugin version {running}, "
        f"but {installed} is installed. A session keeps the plugin root it "
        f"resolved at its own start, so start a new session to pick it up. "
        f"Capturing from a root that does not match the install writes rows the "
        f"current rules would refuse — and a root NEWER than the install is "
        f"just as wrong, because rolling the install back is how a bad release "
        f"is stopped."
    )


def installed_version(manifest: Path | None = None) -> str:
    """The version of the installed clone, or "" if it cannot be read.

    ZOTERO_PROVENANCE_INSTALLED_MANIFEST overrides the location. Two callers
    need that: an end-to-end test, which runs the hook in a subprocess and must
    not be at the mercy of what this machine happens to have installed, and
    anyone whose plugin lives somewhere other than the marketplace clone.
    """
    if manifest is None:
        override = os.environ.get("ZOTERO_PROVENANCE_INSTALLED_MANIFEST")
        manifest = Path(override) if override else INSTALLED_MANIFEST
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError) as e:
        logger.debug("could not read installed version from %s: %s", manifest, e)
        return ""
