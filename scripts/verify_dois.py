#!/usr/bin/env python3
"""Check the DOIs in your library against CrossRef and Retraction Watch.

A provenance library records what was consulted. It does not, by itself, notice
that one of those sources was retracted six months ago. This asks.

    python3 scripts/verify_dois.py --dry-run   # how many DOIs are there
    python3 scripts/verify_dois.py             # check them

`ghostcite` does the work — CrossRef byline cross-check, a Retraction Watch
snapshot, optional PubMed/OpenAlex corroboration — because it is audited and a
second implementation of that check is a second thing to be wrong. Install it
with `pipx install ghostcite`.

Read-only. It reports; it does not tag, trash or rewrite anything. What to do
about a retracted source is a judgement about your own bibliography, and the
tool that finds it is the wrong place to make that call automatically.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.doi_gate import (  # noqa: E402
    doi_of,
    format_gate_report,
    run_ghostcite,
)
from zotero_capture.logging_setup import configure_cli_logging  # noqa: E402


def main() -> int:
    configure_cli_logging()
    p = argparse.ArgumentParser(prog="verify-dois")
    p.add_argument("--dry-run", action="store_true", help="count, do not check")
    p.add_argument("--limit", type=int, default=None, help="only the first N DOIs")
    p.add_argument(
        "--max-rps", type=float, default=2.0, help="cap outbound requests per second"
    )
    args = p.parse_args()

    db_path = load_config().db_path
    with sqlite3.connect(db_path) as conn:
        urls = [r[0] for r in conn.execute("SELECT url_canonical FROM url_index")]

    by_url: dict[str, str] = {}
    for url in urls:
        doi = doi_of(url)
        # First URL wins: two spellings of one DOI are one source, and reporting
        # the finding twice would overstate how much is wrong.
        if doi and doi not in by_url:
            by_url[doi] = url

    dois = list(by_url)[: args.limit] if args.limit is not None else list(by_url)
    print(f"library rows : {len(urls)}")
    print(f"DOIs found   : {len(by_url)}")
    if args.dry_run:
        return 0
    if not dois:
        print("nothing to check")
        return 0

    result = run_ghostcite(dois, max_rps=args.max_rps)
    for line in format_gate_report(result, by_url=by_url):
        print(line)
    # A tool that could not run is not a pass, and the exit code says so.
    return 2 if result.unavailable else 0


if __name__ == "__main__":
    raise SystemExit(main())
