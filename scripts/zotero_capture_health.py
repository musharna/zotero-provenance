#!/usr/bin/env python3
"""Print capture-health warnings, or print nothing at all.

Deliberately does NOT load credentials. Health is a question about the log and
the plugin registry, and requiring a working config would make the check go
quiet in some of the cases it exists to report.

Exits 0 no matter what: this runs from a SessionStart hook, and a health check
that can break a session start is a worse bug than the ones it looks for.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.config import _state_dir  # noqa: E402
from zotero_capture.health import evaluate  # noqa: E402

DEFAULT_MAX_SILENCE_HOURS = 24.0


def _installed() -> tuple[str | None, datetime | None]:
    """Where the plugin manager points, and when it last pointed somewhere new.

    None means "do not guess": an unreadable registry must not be reported as a
    version mismatch. The timestamp matters as much as the path — without it,
    the check fires on every release, because for a few minutes after an upgrade
    the newest capture legitimately came from the previous root.
    """
    registry = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(registry.read_text())
    except (OSError, ValueError):
        return None, None
    for name, entries in (data.get("plugins") or {}).items():
        if not name.startswith("zotero-provenance@"):
            continue
        for entry in entries or []:
            path = entry.get("installPath")
            if not path:
                continue
            when = None
            raw = entry.get("lastUpdated") or entry.get("installedAt")
            if isinstance(raw, str):
                try:
                    when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    when = None
                if when is not None and when.tzinfo is None:
                    when = None
            return str(path), when
    return None, None


def _max_silence() -> timedelta:
    raw = os.environ.get("ZOTERO_CAPTURE_MAX_SILENCE_HOURS")
    try:
        hours = float(raw) if raw else DEFAULT_MAX_SILENCE_HOURS
    except ValueError:
        hours = DEFAULT_MAX_SILENCE_HOURS
    return timedelta(hours=max(hours, 0.0))


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
        max_silence=_max_silence(),
        installed_at=installed_at,
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
