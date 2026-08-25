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
from zotero_capture.health import evaluate, latest_record_ts  # noqa: E402
from zotero_capture.registry import resolve_pinned  # noqa: E402

ACK_FILE = "health-ack"


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


def _acknowledged_before(state: Path) -> datetime | None:
    """The newest record already reported, so an incident is said once."""
    try:
        raw = (state / ACK_FILE).read_text().strip()
    except OSError:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _acknowledge(state: Path, when: datetime | None) -> None:
    if when is None:
        return
    try:
        state.mkdir(parents=True, exist_ok=True)
        (state / ACK_FILE).write_text(when.isoformat())
    except OSError:
        pass


def main() -> int:
    log = _state_dir(os.environ) / "capture.log"
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return 0

    state = _state_dir(os.environ)
    pinned, installed_at = _installed()
    warnings = evaluate(
        lines,
        pinned_root=pinned,
        now=datetime.now().astimezone(),
        installed_at=installed_at,
        acknowledged_before=_acknowledged_before(state),
    )
    if not warnings:
        return 0

    print("zotero-provenance: capture may not be working")
    for warning in warnings:
        print(f"  - {warning}")
    # Everything examined is now reported. A record written after this point is
    # newer than the cursor and will still be raised.
    _acknowledge(state, latest_record_ts(lines))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # A crashed monitor is UNHEALTHY, and must not look like a quiet one.
        # This used to exit 0 after writing to a stderr the hook discards, so an
        # internal fault was byte-identical to a clean bill of health — the very
        # failure class this check exists to report. The traceback goes to the
        # caller's stderr, which the hook diverts to a log; the hook turns the
        # non-zero exit into one stable sentence.
        import traceback

        traceback.print_exc()
        raise SystemExit(3) from None
