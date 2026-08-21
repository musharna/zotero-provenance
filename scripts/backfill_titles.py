#!/usr/bin/env python3
"""Repair items captured before title re-enrichment existed.

Capture only retries a title when its URL is cited again, so items that failed
earlier would otherwise wait for a recurrence that may never come. Run this once:

    python3 scripts/backfill_titles.py --dry-run --limit 100   # measure first
    python3 scripts/backfill_titles.py                         # then repair

Credentials come from the usual secrets file. Set GITHUB_TOKEN to lift GitHub's
anonymous 60-requests/hour limit before repairing a large repo backlog.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.backfill import backfill  # noqa: E402
from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.title_fetcher import fetch_title  # noqa: E402

# The Stop hook's 1s budget exists to keep a turn snappy. This runs unattended,
# so give slow identifier APIs room to answer instead of failing them for speed.
BACKFILL_TIMEOUT_S = 8.0


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
    args = p.parse_args()

    config = load_config()
    started = time.monotonic()

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
        httpx.Client(timeout=BACKFILL_TIMEOUT_S, follow_redirects=True) as http,
        build_client(config) as zotero,
    ):
        result = backfill(
            zotero,
            lambda url: fetch_title(url, client=http),
            dry_run=args.dry_run,
            limit=args.limit,
            sleep_s=args.sleep,
            progress=report,
        )

    verb = "would fix" if args.dry_run else "fixed"
    count = result.would_fix if args.dry_run else result.fixed
    print(f"examined         : {result.examined}")
    print(f"{verb:<17}: {count}")
    print(f"still unresolved : {result.still_unresolved}")
    print(f"errors           : {result.errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
