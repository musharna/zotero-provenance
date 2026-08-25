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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.config import _state_dir  # noqa: E402
from zotero_capture.health import evaluate, incident_keys  # noqa: E402
from zotero_capture.registry import resolve_pinned  # noqa: E402

ACK_FILE = "health-acknowledged"
DEFAULT_WINDOW_HOURS = 24.0


def _installed() -> tuple[str | None, datetime | None]:
    """Where the plugin manager points, and when it last pointed somewhere new.

    Delegated to `registry.resolve_pinned`, which matches the qualified plugin
    id exactly rather than the first key with the right prefix. None means "do
    not guess": an unreadable or ambiguous registry must never be reported as a
    version mismatch. Records that carry their own `pinned_root` no longer
    depend on this at all — they were already proof.
    """
    registry = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    root, when = resolve_pinned(
        own_root=Path(__file__).resolve().parent.parent, registry_path=registry
    )
    return (str(root) if root else None), when


def _window() -> timedelta:
    raw = os.environ.get("ZOTERO_CAPTURE_HEALTH_WINDOW_HOURS")
    try:
        hours = float(raw) if raw else DEFAULT_WINDOW_HOURS
    except ValueError:
        hours = DEFAULT_WINDOW_HOURS
    return timedelta(hours=max(hours, 0.0))


def _acknowledged(state: Path) -> frozenset[str]:
    try:
        return frozenset(
            line.strip()
            for line in (state / ACK_FILE).read_text().splitlines()
            if line.strip()
        )
    except FileNotFoundError:
        return frozenset()
    except OSError:
        # Unreadable is not empty. Saying "nothing acknowledged" would re-report
        # incidents the user has already handled, which trains them to ignore it.
        raise


def _read_log(state: Path) -> list[str]:
    """The log's lines, or [] only when there is genuinely no log yet.

    Every OSError used to mean "return 0 and print nothing", which made a
    permission error, a directory in place of the file, or a failing disk
    indistinguishable from a clean bill of health. A monitor that cannot reach
    its evidence is not healthy; only a missing file is.
    """
    try:
        with (state / "capture.log").open("r", errors="replace") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return []


def main() -> int:
    state = _state_dir(os.environ)
    ack_mode = "--ack" in sys.argv[1:]
    lines = _read_log(state)
    pinned, _installed_at = _installed()

    if ack_mode:
        keys = incident_keys(lines, pinned_root=pinned)
        state.mkdir(parents=True, exist_ok=True)
        (state / ACK_FILE).write_text("".join(f"{key}\n" for key in sorted(keys)))
        print(f"zotero-provenance: acknowledged {len(keys)} integrity incident(s)")
        return 0

    warnings = evaluate(
        lines,
        pinned_root=pinned,
        now=datetime.now().astimezone(),
        window=_window(),
        acknowledged=_acknowledged(state),
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
    except Exception:
        # A crashed monitor is UNHEALTHY and must not look like a quiet one.
        import traceback

        traceback.print_exc()
        raise SystemExit(3) from None
