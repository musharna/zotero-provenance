#!/usr/bin/env python3
"""Compare the index against the library, in BOTH directions. Read-only.

Every other maintenance tool here iterates the index: repair, retire, prune,
snapshot, backfill all start from `SELECT ... FROM url_index` and ask Zotero
about each row. That means none of them can see an item the index does not name,
and so a whole class of drift accumulated for months without any tool being able
to report it -- 49 stranded items, 30 of which had already caused a DUPLICATE,
found only when the collection was finally paged the other way.

The direction is the entire point. A check that walks one store and looks the
rows up in the other verifies that store's rows and says nothing whatever about
the rows it never enumerated. That is the same shape as the exclusion-rule
guard that "passed" because a fallthrough named every row, and the same shape as
the User-Agent guard that could not fail on a call site written after it.

Nothing here writes. It prints what disagrees and exits; deciding what to do
about a duplicate is a judgement call, and this project has learned that
generalising such a call into a rule deletes real citations.

    python scripts/verify_index.py
    python scripts/verify_index.py --strict     # exit 1 if anything disagrees
    python scripts/verify_index.py --show 20    # more examples per section
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.capture import STALE_CLAIM_S  # noqa: E402
from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import load_config  # noqa: E402
from zotero_capture.logging_setup import configure_cli_logging  # noqa: E402
from zotero_capture.sqlite_cache import init_db  # noqa: E402


def _rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with closing(conn):
        return [
            dict(r)
            for r in conn.execute(
                "SELECT url_canonical, zotero_key, pending_key, first_seen,"
                " claimed_at FROM url_index"
            )
        ]


def claim_age_note(row: dict, *, now: datetime) -> str:
    """How long a claim has stood, and what that means.

    "claims still in flight" covered a 12-second claim and a six-day one with
    the same words, and two live rows sat for five and six days under that
    heading. Past STALE_CLAIM_S the claim is not in flight: its POST was cut
    off, and the tool that settles it is named.
    """
    claimed_at = row.get("claimed_at") or ""
    if not claimed_at:
        return "claimed before 0.10.0; no timestamp"
    age = (now - datetime.fromisoformat(claimed_at)).total_seconds()
    if age < STALE_CLAIM_S:
        return f"claimed {int(age)}s ago; in flight"
    if age < 3600:
        span = f"{int(age // 60)}m"
    elif age < 86400:
        span = f"{int(age // 3600)}h"
    else:
        span = f"{int(age // 86400)}d"
    return f"claimed {span} ago; cut off -- drain_queue settles it"


def compare(rows: list[dict], items: list[dict]) -> dict[str, list]:
    """Everything the two stores disagree about. Pure, so it is testable.

    Buckets are disjoint and are asserted to sum, in both directions. A report
    whose parts do not add up to its population is how "nothing is stranded"
    got said about a set nobody had counted.
    """
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[r["zotero_key"]].append(r)
    indexed_urls = {r["url_canonical"] for r in rows}
    pending = {
        r["pending_key"] for r in rows if not r["zotero_key"] and r["pending_key"]
    }
    item_keys = {i["key"] for i in items}

    out: dict[str, list] = {
        "url_disagreement": [],
        "orphan_duplicate": [],
        "orphan_stranded": [],
        "orphan_pending_claim": [],
        "row_item_missing": [],
        "row_no_key": [],
    }

    for it in items:
        rows_for_item = by_key.get(it["key"])
        if not rows_for_item:
            # An item nothing indexes. `lookup_url` cannot find it, so the next
            # citation of this URL creates a SECOND item -- which is why the
            # duplicate bucket exists and why it is worth separating.
            if it["key"] in pending:
                out["orphan_pending_claim"].append(it)
            elif it["url"] in indexed_urls:
                out["orphan_duplicate"].append(it)
            else:
                out["orphan_stranded"].append(it)
            continue
        for r in rows_for_item:
            if (it["url"] or "").strip() != r["url_canonical"]:
                out["url_disagreement"].append((r, it))

    for r in rows:
        if not r["zotero_key"]:
            out["row_no_key"].append(r)
        elif r["zotero_key"] not in item_keys:
            # Either trashed (Zotero's trash is a FLAG: the item reads 200 OK
            # with deleted:1 and simply drops out of the collection listing) or
            # genuinely deleted. Both mean the index claims a source the library
            # no longer shows.
            out["row_item_missing"].append(r)

    counted = (
        len(out["orphan_duplicate"])
        + len(out["orphan_stranded"])
        + len(out["orphan_pending_claim"])
    )
    assert counted == len([i for i in items if i["key"] not in by_key]), (
        "orphan buckets do not sum to the orphans; the report would be lying "
        "about a population it did not enumerate"
    )
    return out


def main(argv: list[str] | None = None) -> int:
    configure_cli_logging()
    p = argparse.ArgumentParser(prog="verify-index")
    p.add_argument("--db-path", default=None)
    p.add_argument("--show", type=int, default=8, help="examples per section")
    p.add_argument(
        "--strict", action="store_true", help="exit 1 if the stores disagree"
    )
    args = p.parse_args(argv)

    config = load_config()
    db_path = Path(args.db_path) if args.db_path else config.db_path
    init_db(db_path)
    rows = _rows(db_path)

    items = []
    with build_client(config, timeout=60.0) as z:
        for it in z.iter_collection_items(limit=100):
            d = it.get("data", {})
            items.append(
                {
                    "key": d.get("key", ""),
                    "url": (d.get("url") or "").strip(),
                    "title": (d.get("title") or "")[:70],
                    "dateAdded": d.get("dateAdded", ""),
                }
            )

    found = compare(rows, items)
    print(f"index rows: {len(rows)}    collection items: {len(items)}")

    sections = [
        (
            "url_disagreement",
            "index row and item hold DIFFERENT urls",
            "the two copies of one identity have drifted apart",
        ),
        (
            "orphan_duplicate",
            "items nothing indexes, whose url IS indexed elsewhere",
            "already duplicated: a second item exists for a captured url",
        ),
        (
            "orphan_stranded",
            "items nothing indexes",
            "invisible to every index-driven tool; citing the url again "
            "creates a duplicate",
        ),
        (
            "orphan_pending_claim",
            "items held by an unresolved claim",
            "NOT a fault: _resolve_claim completes these on the next citation",
        ),
        (
            "row_item_missing",
            "index rows whose item is trashed or deleted",
            "the index claims a source the library no longer shows",
        ),
        (
            "row_no_key",
            "index rows with no zotero key",
            "in flight if seconds old; older than that, the POST was cut off",
        ),
    ]
    disagreements = 0
    for name, heading, why in sections:
        hits = found[name]
        if name not in ("orphan_pending_claim", "row_no_key"):
            disagreements += len(hits)
        print(f"\n{len(hits):5}  {heading}")
        print(f"       ({why})")
        for h in hits[: args.show]:
            if name == "url_disagreement":
                r, it = h
                print(f"       {it['key']}  index: {r['url_canonical'][:72]}")
                print(f"       {'':8}  item : {it['url'][:72]}")
            elif name.startswith("orphan"):
                print(
                    f"       {h['key']}  added {h['dateAdded'][:10]}  {h['url'][:64]}"
                )
            elif name == "row_no_key":
                note = claim_age_note(h, now=datetime.now(timezone.utc))
                print(f"       {'(none)':10}  {h['url_canonical'][:64]}  {note}")
            else:
                print(
                    f"       {h['zotero_key'] or '(none)':10}  {h['url_canonical'][:64]}"
                )
        if len(hits) > args.show:
            print(f"       ... and {len(hits) - args.show} more")

    print(f"\ndisagreements: {disagreements}")
    return 1 if (args.strict and disagreements) else 0


if __name__ == "__main__":
    raise SystemExit(main())
