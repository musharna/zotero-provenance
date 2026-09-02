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

So an old root declines to write and says why. Loud absence beats quiet
corruption — the same trade this plugin makes everywhere else.

The limit is worth stating plainly rather than discovering later: **a guard
cannot fix a version that predates it.** v0.3.0 will never refuse itself.

This file used to conclude from that: "roots older than this release have to be
removed from disk instead, because there is no way to reason with them." That
was wrong twice over, and 0.13.0 corrects it.

Removing them was tried on 2026-08-24 and made things worse — the registry still
pinned the oldest root, so neutering it took capture down for every session for
29 hours. And refusing, even correctly, strands every live session until it
restarts, because a session cannot be made to re-resolve its root.

The move both readings missed is that you do not have to reason with old CODE to
replace the ENTRY POINT that reaches it. The hook SCRIPT is re-read from disk on
every fire, so a trampoline at the top of it hands the work to whichever root the
plugin manager currently pins — see hooks/capture-stop.sh. An old root now
delegates rather than declines, and nothing has to be deleted or restarted.

This guard stays as defence in depth: it is what stops a write when forwarding
cannot resolve a target at all.

For that to be worth anything, the guard and the trampoline have to be answering
the SAME question from the SAME place, and until 0.49.0 they were not. The
trampoline asked the registry which root to run; this module asked the
marketplace clone what version was installed. Two files, one fact — and on
2026-09-01 they disagreed for the eleven hours between their mtimes, so four
captures were refused by the root the registry had pinned. A guard that
contradicts the mechanism it backs up is not defence in depth, it is a second
opinion; both now read `installed_plugins.json` through `registry.resolve_pinned`.

Worth stating because the comment that used to sit on the deleted constant got
it backwards: being the source something was BUILT from does not make you the
authority on what is DEPLOYED.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .registry import resolve_pinned

logger = logging.getLogger(__name__)

# Where this module's own root is, so the registry can be asked whether it is
# the pinned one. Why the registry and not the marketplace clone is the module
# docstring's business, above; repeating it here would be one more second copy.
OWN_ROOT = Path(__file__).resolve().parent.parent.parent

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


def _pinned_manifest() -> Path | None:
    """The manifest of the root the registry pins, or None to not guess.

    None is deliberately indistinguishable from "cannot read the registry":
    `resolve_pinned` documents None as "do not guess", never "nothing is
    installed", and the caller must carry that through as UNKNOWN.
    """
    try:
        root, _ = resolve_pinned(own_root=OWN_ROOT)
    except Exception as e:  # bookkeeping must never break a capture
        logger.debug("could not resolve the pinned root: %s", e)
        return None
    if root is None:
        logger.debug("the registry pins no unambiguous root")
        return None
    return root / ".claude-plugin" / "plugin.json"


def installed_version() -> str:
    """The version of the root the plugin manager pins, or "" when unknown.

    "" means UNKNOWN, never "nothing is installed" -- `classify` turns it into
    UNKNOWN, which does not refuse. Making an unreadable registry a refusal
    would switch capture off for anyone whose install layout this code cannot
    parse: worse than the stale write it prevents, and invisible the same way.

    ZOTERO_PROVENANCE_INSTALLED_MANIFEST still overrides the location, because
    the end-to-end tests run the hook in a subprocess and must not be at the
    mercy of what this machine happens to have installed. It is now the ONLY
    override: the former `manifest` parameter was passed by nothing anywhere,
    while its docstring claimed two callers needed it.
    """
    override = os.environ.get("ZOTERO_PROVENANCE_INSTALLED_MANIFEST")
    manifest = Path(override) if override else _pinned_manifest()
    if manifest is None:
        return ""
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, ValueError) as e:
        logger.debug("could not read installed version from %s: %s", manifest, e)
        return ""
