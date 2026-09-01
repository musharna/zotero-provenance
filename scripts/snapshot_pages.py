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
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.snapshot import (  # noqa: E402
    format_snapshot_report,
    format_verify_report,
    HASH_MAX_BYTES,
    hash_page,
    page_is_visible,
    snapshot,
    verify,
)
from zotero_capture.title_fetcher import build_fetch_client  # noqa: E402
from zotero_capture.logging_setup import configure_cli_logging  # noqa: E402

FETCH_TIMEOUT_S = 15.0
ZOTERO_TIMEOUT_S = 30.0
# Per HOST, not per page. The corpus spans ~960 hosts, so a per-run delay would
# wait between unrelated sites for nothing while still letting a burst of pages
# from one small site go out back to back.
DEFAULT_SLEEP_S = 2.0


def main() -> int:
    configure_cli_logging()
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
    p.add_argument(
        "--only-host",
        help="re-attempt only rows on this host and its subdomains (e.g."
        " 'wikipedia.org' takes en.wikipedia.org but not notwikipedia.org), so a"
        " fix that affects one host family can be proved on it before a wide re-run",
    )
    p.add_argument(
        "--only-outcome",
        help="re-attempt only rows whose last read ended this way (e.g. 'gone'),"
        " so a change to one classification can be re-run without disturbing"
        " hosts that have nothing to do with it",
    )
    p.add_argument(
        "--max-bytes",
        type=int,
        default=HASH_MAX_BYTES,
        help="how much of each document to hash (default %(default)s). A page"
        " larger than this is hashed to exactly its first N bytes and recorded"
        " as covering only that, never as a whole-document hash",
    )
    args = p.parse_args()

    config = load_config()
    db_path = config.db_path
    # Read per page inside the loop: `hashed_at` is when THAT page was read,
    # and a run over the whole corpus takes hours.
    def clock() -> str:
        return datetime.now(timezone.utc).isoformat()

    with build_fetch_client(timeout=FETCH_TIMEOUT_S) as http:

        def hasher(url: str, max_bytes: int):
            return hash_page(url, client=http, max_bytes=max_bytes)

        def visible(url: str) -> bool:
            """Is this URL visible to an anonymous reader? Used only to decide
            whether a 404 is evidence of absence -- see `absence_is_corroborated`.

            Sleeps first: this is always a second request to the SAME host as the
            one that just 404'd, so firing it immediately would break the spacing
            the run promises. Any error other than a clean 404/410 answers False,
            because the question is "did we SEE it", and a timeout did not.
            """
            if args.sleep:
                time.sleep(args.sleep)
            return page_is_visible(url, client=http)

        if args.verify:
            for line in format_verify_report(
                verify(
                    db_path,
                    hasher=hasher,
                    limit=args.limit,
                    sleep_s=args.sleep,
                    max_bytes=args.max_bytes,
                )
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
                    only_outcome=args.only_outcome,
                    only_host=args.only_host,
                    sleep_s=args.sleep,
                    max_bytes=args.max_bytes,
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
                only_outcome=args.only_outcome,
                only_host=args.only_host,
                sleep_s=args.sleep,
                max_bytes=args.max_bytes,
                visible=visible,
            )

    for line in format_snapshot_report(result, dry_run=False):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
