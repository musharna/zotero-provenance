#!/usr/bin/env python3
"""Repair index rows whose URL kept the markdown the old extractor left on it.

Dry run by default: it prints the plan and changes nothing. Pass --apply to
carry it out. Retiring a duplicate uses Zotero's trash, which is recoverable
from any client, never the permanent DELETE.

    python scripts/repair_urls.py            # show the plan
    python scripts/repair_urls.py --apply    # do it
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.repair import apply_repair, plan_repair  # noqa: E402
from zotero_capture.sqlite_cache import init_db  # noqa: E402


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _read_rows(db_path: Path) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT url_canonical, zotero_key FROM url_index")
        ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="repair-urls")
    p.add_argument("--apply", action="store_true", help="carry out the plan")
    p.add_argument("--db-path", default=None)
    p.add_argument("--limit", type=int, default=None, help="only the first N steps")
    args = p.parse_args(argv)
    # `if args.limit:` -- 0 is falsy, so the cap on a bulk DESTRUCTIVE run
    # inverted into "no cap" at exactly the value someone reaches for when they
    # want to be careful. A negative one is worse than useless: steps[:-1] is
    # every step but the last.
    if args.limit is not None and args.limit < 0:
        p.error("--limit must not be negative")

    config = load_config()
    db_path = Path(args.db_path) if args.db_path else config.db_path
    # Bring the schema up to date first. This tool can run against an index
    # written by an older release, and the repair reads columns that arrived by
    # migration — without this every merge failed on "no such column".
    init_db(db_path)
    rows = _read_rows(db_path)
    steps = plan_repair(rows)
    if args.limit is not None:
        steps = steps[: args.limit]

    by_action: dict[str, int] = {}
    for s in steps:
        by_action[s.action] = by_action.get(s.action, 0) + 1
    print(f"index rows: {len(rows)}")
    for action in ("rewrite", "merge", "skip"):
        print(f"  {action:8} {by_action.get(action, 0)}")

    for s in steps:
        if s.action == "skip":
            print(f"  SKIP    {s.url}  ({s.reason})")
        else:
            print(f"  {s.action.upper():7} {s.url}\n          -> {s.corrected}")

    if not args.apply:
        print("\nDry run. Nothing was changed. Re-run with --apply to carry this out.")
        return 0

    with build_client(config, timeout=30.0) as zotero:
        counts = apply_repair(
            steps,
            db_path=db_path,
            zotero=zotero,
            connect=lambda p: closing(_connect(p)),
        )
    print(f"\napplied: {counts}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
