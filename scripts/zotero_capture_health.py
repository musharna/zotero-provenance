#!/usr/bin/env python3
"""Print capture-health warnings, or print nothing at all.

Deliberately does NOT load credentials. Health is a question about the log and
the plugin registry, and requiring a working config would make the check go
quiet in some of the cases it exists to report.

Exits 0 no matter what: this runs from a SessionStart hook, and a health check
that can break a session start is a worse bug than the ones it looks for.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.config import _state_dir  # noqa: E402
from zotero_capture.health import evaluate, incidents  # noqa: E402
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


@contextlib.contextmanager
def _log_lines(state: Path):
    """Stream the log. Only a MISSING log is healthy silence.

    Every other OSError used to mean exit 0 and nothing printed, so a permission
    error, a directory in place of the file, or a failing disk was
    indistinguishable from a clean bill of health. And this used to call
    `read().splitlines()`, which held the whole file before parsing began —
    about 120 MB for half a million lines, defeating the streaming evaluator
    behind it.
    """
    try:
        handle = (state / "capture.log").open("r", errors="replace")
    except FileNotFoundError:
        yield iter(())
        return
    try:
        yield handle
    finally:
        handle.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="zotero_capture_health",
        description="Report capture faults. Silent when there is nothing to say.",
    )
    parser.add_argument(
        "--list-incidents", action="store_true",
        help="show each integrity incident and its id",
    )
    parser.add_argument(
        "--ack", nargs="+", metavar="ID", default=None,
        help="acknowledge the named incident(s); see --list-incidents",
    )
    parser.add_argument(
        "--ack-all", action="store_true",
        help="acknowledge EVERY incident in the log, including any not shown",
    )
    return parser.parse_args(argv)


def _write_ack(state: Path, keys: set[str]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / ACK_FILE).write_text("".join(f"{key}\n" for key in sorted(keys)))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    state = _state_dir(os.environ)
    pinned, _installed_at = _installed()

    if args.list_incidents:
        with _log_lines(state) as lines:
            found = incidents(lines, pinned_root=pinned)
        if not found:
            print("zotero-provenance: no integrity incidents recorded")
            return 0
        acked = _acknowledged(state)
        for item in found:
            mark = "acknowledged" if item["id"] in acked else "OPEN"
            print(f"{item['id']}  {item['ts']}  {item['kind']:<10} {mark}  {item['root']}")
        return 0

    if args.ack_all:
        with _log_lines(state) as lines:
            keys = {item["id"] for item in incidents(lines, pinned_root=pinned)}
        _write_ack(state, keys | set(_acknowledged(state)))
        print(f"zotero-provenance: acknowledged all {len(keys)} incident(s)")
        return 0

    if args.ack is not None:
        # Only the ids named. Acknowledging everything used to be what a bare
        # --ack did, silently, including incidents the aggregated report never
        # displayed — it was an undocumented --ack-all.
        _write_ack(state, set(args.ack) | set(_acknowledged(state)))
        print(f"zotero-provenance: acknowledged {len(set(args.ack))} incident(s)")
        return 0

    with _log_lines(state) as lines:
        warnings = evaluate(
            lines,
            pinned_root=pinned,
            now=datetime.now().astimezone(),
            window=_window(),
            acknowledged=_acknowledged(state),
        )
    if not warnings:
        return 0

    print("zotero-provenance: capture faults were recorded")
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
