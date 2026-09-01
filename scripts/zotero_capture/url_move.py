"""Moving a URL, which means moving it in BOTH stores or in neither.

A captured URL is held twice: `item.url` in the Zotero library, and
`url_canonical` in the index, where it is the PRIMARY KEY. They are one
identity with two copies, and changing one alone silently desynchronises them.

That is not hypothetical. On 2026-08-22 a one-off script in a scratch directory
did exactly this to 24 rows:

    with build_client(load_config(), timeout=30.0) as z:
        for o in reps:
            z.update_url(o["key"], o["new"])      # the item. and nothing else.

The library became right and the index stayed wrong. Nine days later `snapshot`
fetched the stale index strings, got 404s, and recorded `gone` -- so a repair
manufactured the link rot the tool exists to report truthfully.

`repair.py` had always done both writes, in the right order, with the right
guards. But that was a CONVENTION living in one function's body, and a
convention cannot bind a script somebody writes at 2am against the client
directly. So the pairing moved here, into an operation whose signature cannot be
satisfied without both stores, and the client's one-store URL write became
private. The next scratch script that reaches for the obvious API gets this one,
because it is the only one there is.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def move_url(
    db_path: Path,
    zotero: Any,
    *,
    zotero_key: str,
    old: str,
    new: str,
    connect: Callable[[Path], Any],
) -> str:
    """Move a URL in the library and the index. "" on success, else the reason.

    The item is written FIRST and the index only if that succeeded. The order is
    load-bearing and the failure is asymmetric:

      * item written, index not  -> the index points at a URL the library has
        moved past. Recoverable: the item of record still holds the truth, and
        the row can be made to follow it. This is what actually happened, and
        repairing it in 0.41.0 was bookkeeping.
      * index written, item not  -> the index claims a URL with nothing behind
        it, and the address that WAS captured is gone from both stores. Nothing
        can recover it.

    So the write that can be undone goes first. A refusal returns a reason
    rather than raising: every caller is a bulk pass over many rows, and one
    row's stale plan must not end the run.
    """
    if not zotero_key:
        return "row has no zotero key"
    if old == new:
        return "already at that url"

    # expect_url is carried HERE rather than left to callers. Every caller
    # selects from a snapshot and writes later by key, and optimistic versioning
    # does not cover that gap -- it stops a write racing the final GET, not an
    # edit that landed before it.
    if not zotero._patch_item_url(zotero_key, new, expect_url=old):
        # NOT success. Rewriting the index row anyway would leave it claiming a
        # corrected URL with nothing behind it, and the pass would report a
        # repair that did not happen.
        logger.warning(
            "not moving %s: item %s is gone, trashed, or no longer that item",
            old,
            zotero_key,
        )
        return "item is gone, trashed, or no longer the one selected"

    # ...AND zotero_key: between the plan and here another session can have
    # replaced this row, and rewriting by URL alone moves a row that belongs to
    # a different item.
    with connect(db_path) as conn:
        moved = conn.execute(
            "UPDATE url_index SET url_canonical = ?"
            " WHERE url_canonical = ? AND zotero_key = ?",
            (new, old, zotero_key),
        ).rowcount
    if not moved:
        logger.warning(
            "moved item %s but its index row was replaced; leaving the new row alone",
            zotero_key,
        )
        return "index row was replaced"

    # The queue is keyed by URL and the row just moved out from under it. Left
    # behind, those sightings sit under an address no index row will ever
    # revisit: the recurring branch only runs for a URL that is in the index,
    # and this one no longer is.
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pending_tags (url_canonical, tag)"
            " SELECT ?, tag FROM pending_tags WHERE url_canonical = ?",
            (new, old),
        )
        conn.execute("DELETE FROM pending_tags WHERE url_canonical = ?", (old,))
    return ""
