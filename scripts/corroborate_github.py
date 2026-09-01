#!/usr/bin/env python3
"""Ask GitHub whether the repos we call dead actually exist. Dry run by default.

GitHub 404s a private repository on purpose, so anonymously "deleted" and "not
yours to see" are the same answer. 0.39.0 discriminated the rows whose immediate
parent is itself hidden, but a repo ROOT's parent is a public profile page, which
is visible either way -- leaving rows that assert the owner's own private
repositories are dead links.

A credential settles it, and this is the ONLY place one is used. Capture and
snapshot stay credential-free; nothing here fetches, hashes, or stores repository
content, and the only thing it can change is an outcome label on a URL the
library already holds.

It can only DOWNGRADE. `gone` becomes `not_visible` when the repo is there. A
404 -- even with a token -- stays exactly as it was, because a credential that
cannot see it either has proved nothing about the resource.

    python scripts/corroborate_github.py            # show what it would change
    python scripts/corroborate_github.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.github_visibility import (  # noqa: E402
    VISIBLE,
    read_token,
    repo_slug,
    repo_visibility,
)
from zotero_capture.logging_setup import configure_cli_logging  # noqa: E402
from zotero_capture.title_fetcher import build_fetch_client  # noqa: E402
from zotero_capture.opjournal import OperationJournal  # noqa: E402
from zotero_capture.snapshot import GONE, NOT_VISIBLE  # noqa: E402
from zotero_capture.sqlite_cache import init_db, set_fetch_outcome  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    configure_cli_logging()
    p = argparse.ArgumentParser(prog="corroborate-github")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--db-path", default=None)
    p.add_argument(
        "--sleep",
        type=float,
        default=0.7,
        help="seconds between API calls (GitHub rate limits)",
    )
    args = p.parse_args(argv)

    cfg = load_config()
    db = Path(args.db_path) if args.db_path else cfg.db_path
    init_db(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    with closing(conn):
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT url_canonical, last_outcome FROM url_index"
                " WHERE last_outcome = ?",
                (GONE,),
            )
        ]

    candidates = [(r, repo_slug(r["url_canonical"])) for r in rows]
    candidates = [(r, s) for r, s in candidates if s]
    print(f"{len(rows)} rows marked {GONE}; {len(candidates)} of them name a repo root")
    if not candidates:
        return 0

    token = read_token()
    if not token:
        print("\nNo GitHub token (set GITHUB_TOKEN or run `gh auth login`).")
        print("Nothing is assumed without one -- every answer would be 'unknown'.")
        return 2

    changed, unknown = [], []
    with build_fetch_client(timeout=20.0) as client:
        for r, slug in candidates:
            v = repo_visibility(slug, client=client, token=token)
            (changed if v == VISIBLE else unknown).append((r, slug))
            print(
                f"  {'EXISTS -> not_visible' if v == VISIBLE else 'unknown, unchanged':22}"
                f" {slug[0]}/{slug[1]}"
            )
            time.sleep(args.sleep)

    print(f"\nwould downgrade {len(changed)}; leaving {len(unknown)} unchanged")
    if not args.apply:
        print("Dry run. Nothing changed. Re-run with --apply.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    done = 0
    with OperationJournal(db, "corroborate-github", args="--apply") as J:
        for r, slug in changed:
            seq = J.step(
                target=f"{slug[0]}/{slug[1]}",
                action="downgrade",
                before={"url": r["url_canonical"], "outcome": r["last_outcome"]},
            )
            # final_url is '' on purpose: it means "we never found out where the
            # request ended", and we did not make a web request at all here.
            if set_fetch_outcome(
                db, r["url_canonical"], outcome=NOT_VISIBLE, at=now, final_url=""
            ):
                J.outcome(seq, "done")
                done += 1
            else:
                J.outcome(seq, "refused", "row disappeared")
    print(f"downgraded: {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
