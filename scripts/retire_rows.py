#!/usr/bin/env python3
"""Retire index rows that can never be a source, trashing the items behind them.

Dry run by default: it prints the plan, grouped by reason, and changes nothing.
Pass --apply to carry it out. Retiring uses Zotero's trash, which is recoverable
from any client, never the permanent DELETE.

    python scripts/retire_rows.py            # show the plan
    python scripts/retire_rows.py --apply    # do it

The companion pass is scripts/repair_urls.py, and the two are deliberately not
one tool: repair recovers a URL that has a right answer, retirement removes one
that has none. Sharing a predicate between them is how a repairable row would
get thrown away instead of fixed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.retire import (  # noqa: E402
    apply_retire,
    journal_path,
    plan_retire,
)
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
    p = argparse.ArgumentParser(prog="retire-rows")
    p.add_argument("--apply", action="store_true", help="carry out the plan")
    p.add_argument(
        "--policy",
        action="store_true",
        help="also retire rows excluded by policy rather than by proof — page\nassets, infrastructure, intranet and private names. These CAN resolve for\nwhoever is on that network, so reaching back and trashing them is a product\ndecision and is opt-in.",
    )
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
    init_db(db_path)
    rows = _read_rows(db_path)
    steps = plan_retire(rows, include_policy=args.policy)
    if args.limit is not None:
        steps = steps[: args.limit]

    by_reason = Counter(s.reason for s in steps)
    if not args.policy:
        held = len(plan_retire(rows, include_policy=True)) - len(steps)
        if held:
            print(f"({held} more are policy exclusions; pass --policy to include them)")
    print(f"index rows: {len(rows)}")
    print(f"to retire:  {len(steps)}  ({len(rows) - len(steps)} left alone)")
    for reason, n in by_reason.most_common():
        print(f"  {n:4}  {reason}")

    for reason, _ in by_reason.most_common():
        print(f"\n-- {reason}")
        for s in steps:
            if s.reason == reason:
                marker = s.zotero_key if s.zotero_key else "(no item)"
                print(f"  {marker:12} {s.url}")

    if not args.apply:
        print("\nDry run. Nothing was changed. Re-run with --apply to carry this out.")
        print("Applied runs journal every removed row to "
              f"{journal_path(db_path).name} first: Zotero's trash restores the item, "
              "not the sighting history.")
        return 0

    with build_client(config, timeout=30.0) as zotero:
        counts = apply_retire(steps, db_path=db_path, zotero=zotero, connect=_connect)
    print(
        f"\ntrashed {counts['trashed']}, rows dropped without an item "
        f"{counts['row_only']}, failed {counts['failed']}"
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
