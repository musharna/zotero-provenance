#!/usr/bin/env python3
"""Retry the captures whose Zotero write failed.

A failed write used to be logged and dropped. Both of capture's own recovery
paths need the URL to be cited AGAIN, so a source cited once, which failed once,
was lost — silently, from a library whose whole purpose is to record what was
actually consulted.

    python3 scripts/drain_queue.py --dry-run    # what is queued
    python3 scripts/drain_queue.py              # work it

Nothing here issues a write of its own: each URL is replayed through
`capture_message`, the same function the Stop hook calls, under its ORIGINAL
sighting date. So a recovered source is filed under the day it was cited, and
the reservation and claim-resolution machinery that already answers "did that
POST commit?" is what answers it here too.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.capture import capture_message  # noqa: E402
from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import _state_dir, load_config  # noqa: E402
from zotero_capture.drain import drain, format_drain_report  # noqa: E402
from zotero_capture.sqlite_cache import RetryEntry, retry_queue_depth  # noqa: E402
from zotero_capture.title_fetcher import build_fetch_client, fetch_title  # noqa: E402
from zotero_capture.logging_setup import configure_cli_logging  # noqa: E402

# Unattended, like the other maintenance passes: give the slow identifier APIs
# room to answer rather than failing them for the hook's interactive budget.
FETCH_TIMEOUT_S = 8.0
ZOTERO_TIMEOUT_S = 30.0


def main() -> int:
    configure_cli_logging()
    p = argparse.ArgumentParser(prog="drain-queue")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what is queued without retrying anything",
    )
    p.add_argument("--limit", type=int, default=None, help="only the first N entries")
    args = p.parse_args()

    # Refused here for the message; sqlite_cache refuses it again for the
    # mechanism. `if args.limit:` is the wrong test -- 0 is a real value.
    if args.limit is not None and args.limit < 0:
        p.error("--limit must be >= 0")

    config = load_config()
    db_path = config.db_path
    ledger_path = _state_dir({}) / "health.db"

    depth = retry_queue_depth(db_path)
    if not depth:
        print("the retry queue is empty")
        return 0
    print(f"queued: {depth}")

    if args.dry_run:
        for line in format_drain_report(
            drain(db_path, replay=_unreachable, dry_run=True, limit=args.limit),
            dry_run=True,
        ):
            print(line)
        return 0

    with (
        build_fetch_client(timeout=FETCH_TIMEOUT_S) as http,
        build_client(config, timeout=ZOTERO_TIMEOUT_S) as zotero,
    ):

        def replay(entry: RetryEntry):
            # The message is the URL alone. capture_message extracts, excludes
            # and canonicalises it exactly as it would from an assistant turn,
            # so a URL that has since become excluded is dropped here too rather
            # than being forced in by a path that skipped the rules.
            return capture_message(
                message=entry["url_canonical"],
                project_slug=entry["project"],
                context=entry["context"],
                today=date.fromisoformat(entry["seen_date"]),
                db_path=db_path,
                zotero=zotero,
                title_fetcher=lambda url: fetch_title(url, client=http),
                origin="assistant",
                ledger_path=ledger_path,
            )

        result = drain(db_path, replay=replay, limit=args.limit)

    for line in format_drain_report(result, dry_run=False):
        print(line)
    return 0


def _unreachable(entry: RetryEntry):  # pragma: no cover - a dry run never replays
    raise AssertionError("a dry run must not replay")


if __name__ == "__main__":
    raise SystemExit(main())
