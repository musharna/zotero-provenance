#!/usr/bin/env python3
"""Repair items captured before title re-enrichment existed.

Capture only retries a title when its URL is cited again, so items that failed
earlier would otherwise wait for a recurrence that may never come. Run this once:

    python3 scripts/backfill_titles.py --dry-run --limit 100   # measure first
    python3 scripts/backfill_titles.py                         # then repair

Pass --host to target a single host after a fix that only helps that host,
instead of re-fetching a backlog that is mostly unfetchable:

    python3 scripts/backfill_titles.py --host en.wikipedia.org --host commons.wikimedia.org

Credentials come from the usual secrets file. Set GITHUB_TOKEN to lift GitHub's
anonymous 60-requests/hour limit before repairing a large repo backlog.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.backfill import backfill  # noqa: E402
from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.prune import format_prune_report, prune  # noqa: E402
from zotero_capture.title_fetcher import (  # noqa: E402
    build_fetch_client,
    fetch_title,
)

# The Stop hook's 1s budget exists to keep a turn snappy. This runs unattended,
# so give slow identifier APIs room to answer instead of failing them for speed.
BACKFILL_TIMEOUT_S = 8.0

# Paging a large collection is the slowest thing here, and Zotero gets slower
# under sustained traffic. The interactive 5s default aborted whole sweeps
# (observed 2026-08-21), so this pass waits instead.
ZOTERO_TIMEOUT_S = 30.0


def main() -> int:
    p = argparse.ArgumentParser(prog="backfill-titles")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch titles and report what would be fixed, without writing",
    )
    p.add_argument("--limit", type=int, default=None, help="stop after N items")
    p.add_argument(
        "--sleep", type=float, default=0.4, help="seconds between items (be polite)"
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="instead of repairing titles, move every item the exclusion rules "
        "reject to the Zotero trash (recoverable). Pair with --dry-run first.",
    )
    p.add_argument(
        "--host",
        action="append",
        dest="hosts",
        metavar="HOSTNAME",
        help="only repair items on this host (repeatable); default is every host",
    )
    args = p.parse_args()
    hosts = {h.lower() for h in args.hosts} if args.hosts else None

    config = load_config()
    started = time.monotonic()

    if args.prune:
        return _run_prune(
            config, dry_run=args.dry_run, limit=args.limit, sleep_s=args.sleep
        )

    def report(r) -> None:
        if r.examined % 25:
            return
        done = r.fixed + r.would_fix
        rate = r.examined / max(time.monotonic() - started, 1e-9)
        print(
            f"  {r.examined} examined | {done} resolved | "
            f"{r.still_unresolved} still unresolved | {r.errors} errors "
            f"| {rate:.1f}/s",
            file=sys.stderr,
            flush=True,
        )

    with (
        # Guarded like every other fetch path: this pass walks thousands of
        # stored URLs unattended, which is exactly where a redirect into a
        # private address would go unnoticed.
        build_fetch_client(timeout=BACKFILL_TIMEOUT_S) as http,
        build_client(config, timeout=ZOTERO_TIMEOUT_S) as zotero,
    ):
        result = backfill(
            zotero,
            lambda url: fetch_title(url, client=http),
            dry_run=args.dry_run,
            limit=args.limit,
            sleep_s=args.sleep,
            hosts=hosts,
            progress=report,
        )

    verb = "would fix" if args.dry_run else "fixed"
    count = result.would_fix if args.dry_run else result.fixed
    print(f"examined         : {result.examined}")
    print(f"{verb:<17}: {count}")
    print(f"still unresolved : {result.still_unresolved}")
    print(f"errors           : {result.errors}")
    return 0


def _run_prune(config, *, dry_run: bool, limit: int | None, sleep_s: float) -> int:
    """Sweep the exclusion rules back over items captured before they existed."""
    with build_client(config, timeout=ZOTERO_TIMEOUT_S) as zotero:
        result = prune(zotero, dry_run=dry_run, limit=limit, sleep_s=sleep_s)

    for line in format_prune_report(result, dry_run=dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
