#!/usr/bin/env python3
"""Hash captured pages, and later ask whether they still say what they said.

A captured item is a URL and a title. If the page is edited, paywalled or taken
down, nothing in the library can show what was actually consulted — the one
thing a provenance record exists to do.

    python3 scripts/snapshot_pages.py --dry-run     # how many lack a hash
    python3 scripts/snapshot_pages.py --limit 200   # hash them
    python3 scripts/snapshot_pages.py --verify      # what has changed since

Unattended by design. `fetch_title` stops reading at `</title>`, so a hash means
reading the whole document, and the Stop hook's budget is the reason the title
fetch is capped at a second in the first place.

A verify pass never rewrites a stored hash. The stored hash is the evidence of
what was consulted; replacing it with what the page says today would destroy the
finding at the moment it was made.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.snapshot import (  # noqa: E402
    format_snapshot_report,
    format_verify_report,
    hash_page,
    snapshot,
    verify,
)
from zotero_capture.title_fetcher import build_fetch_client  # noqa: E402

FETCH_TIMEOUT_S = 15.0
ZOTERO_TIMEOUT_S = 30.0
# Per HOST, not per page. The corpus spans ~960 hosts, so a per-run delay would
# wait between unrelated sites for nothing while still letting a burst of pages
# from one small site go out back to back.
DEFAULT_SLEEP_S = 2.0


def main() -> int:
    p = argparse.ArgumentParser(prog="snapshot-pages")
    p.add_argument("--dry-run", action="store_true", help="count, do not fetch")
    p.add_argument(
        "--verify",
        action="store_true",
        help="re-read hashed pages and report which have changed",
    )
    p.add_argument("--limit", type=int, default=None, help="only the first N rows")
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="also re-attempt rows whose last read failed (blocked, timed out, "
        "rate-limited); by default a recorded failure is left alone",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_S,
        help="minimum seconds between two requests to the SAME host (be polite)",
    )
    args = p.parse_args()

    config = load_config()
    db_path = config.db_path
    # Read per page inside the loop: `hashed_at` is when THAT page was read,
    # and a run over the whole corpus takes hours.
    def clock() -> str:
        return datetime.now(timezone.utc).isoformat()

    with build_fetch_client(timeout=FETCH_TIMEOUT_S) as http:

        def hasher(url: str) -> str:
            return hash_page(url, client=http)

        if args.verify:
            for line in format_verify_report(
                verify(db_path, hasher=hasher, limit=args.limit)
            ):
                print(line)
            return 0

        if args.dry_run:
            for line in format_snapshot_report(
                snapshot(
                    db_path,
                    zotero=None,
                    hasher=hasher,
                    clock=clock,
                    dry_run=True,
                    limit=args.limit,
                    include_failed=args.retry_failed,
                    sleep_s=args.sleep,
                ),
                dry_run=True,
            ):
                print(line)
            return 0

        with build_client(config, timeout=ZOTERO_TIMEOUT_S) as zotero:
            result = snapshot(
                db_path,
                zotero=zotero,
                hasher=hasher,
                clock=clock,
                limit=args.limit,
                include_failed=args.retry_failed,
                sleep_s=args.sleep,
            )

    for line in format_snapshot_report(result, dry_run=False):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
