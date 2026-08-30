#!/usr/bin/env python3
"""Ask what a captured source was actually cited FOR.

    python3 scripts/show_claims.py                     # coverage summary
    python3 scripts/show_claims.py --url doi.org/10.1   # one source's claims
    python3 scripts/show_claims.py --search "retract"   # search the claims

The library answers "was this consulted". This answers "for what" -- the
question an audit of your own bibliography actually asks, six months after the
conversation that produced the citation.

LOCAL ONLY. Claim text is stored in the sqlite index on this machine and is
never written to Zotero, never tagged, never synced. The claim is a fragment of
a conversation; the library is not.

Read-only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.claims import (  # noqa: E402
    CLAIMS_PER_URL,
    claim_counts,
    claims_for_url,
    format_claims,
    search_claims,
)
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.logging_setup import configure_cli_logging  # noqa: E402


def main() -> int:
    configure_cli_logging()
    p = argparse.ArgumentParser(prog="show-claims")
    p.add_argument("--url", default="", help="exact canonical URL to explain")
    p.add_argument("--search", default="", help="substring of a claim or URL")
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()

    db_path = load_config().db_path

    if args.url:
        rows = claims_for_url(db_path, args.url)
        if not rows:
            # Distinguishable from "no such URL": this tool cannot tell the two
            # apart and must not imply it can.
            print(f"no claims recorded for {args.url}")
            print("(the URL may be captured but only ever cited without context)")
            return 0
        for line in format_claims(rows, show_url=False):
            print(line)
        if len(rows) >= CLAIMS_PER_URL:
            print(
                f"\nat the cap of {CLAIMS_PER_URL} distinct claims -- newer ones"
                " for this URL are NOT being recorded"
            )
        return 0

    if args.search:
        rows = search_claims(db_path, args.search, limit=args.limit)
        for line in format_claims(rows):
            print(line)
        print(f"\n{len(rows)} claim(s) matching {args.search!r}")
        return 0

    links, urls = claim_counts(db_path)
    print(f"claim links  : {links}")
    print(f"URLs covered : {urls}")
    if not links:
        print("\nNothing recorded yet. Claims are captured going forward only:")
        print("there is no way to recover the sentence around a URL that was")
        print("cited before this was built -- the conversation is not kept.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
