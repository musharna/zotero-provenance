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
from zotero_capture.health_ledger import (  # noqa: E402
    acknowledge,
    acknowledge_all,
    count_open,
    open_incident,
    open_incidents,
)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list-incidents", action="store_true",
        help="show open integrity incidents and their ids",
    )
    mode.add_argument(
        "--ack", nargs="+", metavar="ID",
        help="resolve the named incident(s); see --list-incidents",
    )
    mode.add_argument(
        "--ack-all", action="store_true",
        help="resolve EVERY open incident, including any not shown",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="how many incidents to list (the remainder is counted, not hidden)",
    )
    return parser.parse_args(argv)


def _migrate_legacy(state: Path, ledger: Path, pinned: str | None) -> None:
    """Import incidents proven by pre-0.19 records, once.

    A 0.15-0.18 record with `root != pinned_root` and evidence of a write
    already proves an integrity incident; only its acknowledgement identity was
    missing. Rejecting it for having no id silently suppressed every open
    incident at the moment of upgrade, which is not the same as rejecting a
    record that cannot prove anything.
    """
    marker = state / "health-migrated"
    if marker.exists():
        return
    try:
        with _log_lines(state) as lines:
            found = incidents(lines, pinned_root=pinned, require_id=False)
        for item in found:
            open_incident(
                ledger, incident_id=item["id"], url=item.get("url"),
                root=item["root"], pinned_root=pinned, kind=item["kind"],
                ts=item["ts"],
            )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{len(found)}\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    state = _state_dir(os.environ)
    ledger = state / "health.db"
    pinned, _installed_at = _installed()
    _migrate_legacy(state, ledger, pinned)
    stamp = datetime.now().astimezone().isoformat()

    if args.list_incidents:
        total = count_open(ledger)
        if not total:
            print("zotero-provenance: no open integrity incidents")
            return 0
        shown = open_incidents(ledger, limit=max(args.limit, 1))
        for item in shown:
            print(
                f"{item['incident_id']}  {item['opened_at']}  "
                f"{item['kind']:<10} {item['root']}  {item.get('url') or ''}"
            )
        if total > len(shown):
            print(f"... {total - len(shown)} more (use --limit to show them)")
        return 0

    if args.ack_all:
        n = acknowledge_all(ledger, now=stamp)
        print(f"zotero-provenance: resolved {n} incident(s)")
        return 0

    if args.ack:
        resolved = acknowledge(ledger, list(args.ack), now=stamp)
        unknown = [i for i in args.ack if i not in resolved]
        for i in resolved:
            print(f"zotero-provenance: resolved {i}")
        for i in unknown:
            # An unknown id used to be appended to a file and reported as
            # success, leaving the real incident open behind a typo.
            print(f"zotero-provenance: no open incident with id {i}", file=sys.stderr)
        return 0 if resolved and not unknown else 1

    with _log_lines(state) as lines:
        warnings = evaluate(
            lines,
            pinned_root=pinned,
            now=datetime.now().astimezone(),
            window=_window(),
            ledger_path=ledger,
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
