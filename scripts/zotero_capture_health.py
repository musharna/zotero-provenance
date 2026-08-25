#!/usr/bin/env python3
"""Print capture-health warnings, or print nothing at all.

Deliberately does NOT load credentials. Health is a question about the log and
the plugin registry, and requiring a working config would make the check go
quiet in some of the cases it exists to report.

Exits 0 no matter what: this runs from a SessionStart hook, and a health check
that can break a session start is a worse bug than the ones it looks for.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.config import _state_dir  # noqa: E402
from zotero_capture.health import evaluate  # noqa: E402
from zotero_capture.registry import resolve_pinned  # noqa: E402

DEFAULT_MAX_FIRES = 200
# Keep the heartbeat bounded without needing a lock: trim only when it grows
# well past what the check reads, and replace atomically.
TRIM_ABOVE, TRIM_TO = 4000, 1000


def _installed() -> tuple[str | None, datetime | None]:
    """Where the plugin manager points, and when it last pointed somewhere new.

    Delegated to `registry.resolve_pinned`, which matches the qualified plugin
    id exactly rather than the first key with the right prefix. The prefix match
    this used to do resolved to whichever entry was serialised first, so an
    ordinary registry holding the plugin from two marketplaces or two scopes
    could name the wrong root.

    None means "do not guess": an unreadable or ambiguous registry must never be
    reported as a version mismatch.
    """
    registry = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    root, when = resolve_pinned(
        own_root=Path(__file__).resolve().parent.parent, registry_path=registry
    )
    return (str(root) if root else None), when


def _max_fires() -> int:
    raw = os.environ.get("ZOTERO_CAPTURE_MAX_FIRES_WITHOUT_CAPTURE")
    try:
        return max(int(raw), 1) if raw else DEFAULT_MAX_FIRES
    except ValueError:
        return DEFAULT_MAX_FIRES


def _hook_fires(state: Path) -> list[str]:
    """Timestamps of recent hook fires, trimming the file if it has grown."""
    path = state / "hook-fires.log"
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    if len(lines) > TRIM_ABOVE:
        keep = lines[-TRIM_TO:]
        tmp = path.with_suffix(".trim")
        try:
            tmp.write_text("".join(f"{line}\n" for line in keep))
            tmp.replace(path)
            lines = keep
        except OSError:
            pass
    return lines


def main() -> int:
    log = _state_dir(os.environ) / "capture.log"
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return 0

    pinned, installed_at = _installed()
    warnings = evaluate(
        lines,
        pinned_root=pinned,
        now=datetime.now().astimezone(),
        installed_at=installed_at,
        fires=_hook_fires(_state_dir(os.environ)),
        max_fires=_max_fires(),
    )
    if not warnings:
        return 0

    print("zotero-provenance: capture may not be working")
    for warning in warnings:
        print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never break a session start
        print(f"zotero-provenance: health check failed to run ({exc})", file=sys.stderr)
        raise SystemExit(0) from None
