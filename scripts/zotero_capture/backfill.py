"""One-off repair pass over items already in the collection.

Capture only re-attempts a title when its URL is cited again, so items that failed
before this existed would wait indefinitely for a recurrence that may never come.
This walks the collection and gives each unresolved item the same retry the
recurrence path performs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .zotero_client import ZoteroClient, title_is_unresolved

logger = logging.getLogger(__name__)

# Zotero asks for a courteous request rate; the wall-clock cost is irrelevant here
# because this runs once, unattended.
DEFAULT_SLEEP_S = 0.4


@dataclass
class BackfillResult:
    examined: int = 0
    fixed: int = 0
    would_fix: int = 0
    still_unresolved: int = 0
    errors: int = 0


def backfill(
    zotero: ZoteroClient,
    title_fetcher: Callable[[str], str],
    *,
    dry_run: bool = False,
    limit: int | None = None,
    sleep_s: float = DEFAULT_SLEEP_S,
    hosts: set[str] | None = None,
    progress: Callable[[BackfillResult], None] | None = None,
) -> BackfillResult:
    """Re-attempt the title of every unresolved item in the collection.

    `hosts` restricts the pass to those hostnames. Most of the backlog is
    structurally unfetchable (publisher WAFs, dead links), so after a fix that
    only helps one host, re-fetching everything spends thousands of requests to
    change a few dozen items.
    """
    result = BackfillResult()
    for item in zotero.iter_collection_items():
        if limit is not None and result.examined >= limit:
            break
        data = item.get("data", {})
        tags = {t["tag"] for t in data.get("tags", [])}
        if not title_is_unresolved(data, tags):
            continue
        url = data.get("url") or ""
        if not url:
            continue
        if hosts is not None and (urlsplit(url).hostname or "").lower() not in hosts:
            continue
        result.examined += 1
        try:
            if dry_run:
                # Still spend the fetch — the point of a dry run is to learn how
                # many would actually be recovered, not merely how many are marked.
                if title_fetcher(url) != url:
                    result.would_fix += 1
                else:
                    result.still_unresolved += 1
            else:
                patched = zotero.add_tags(
                    item["key"], [], title_resolver=lambda u=url: title_fetcher(u)
                )
                if patched:
                    result.fixed += 1
                else:
                    result.still_unresolved += 1
        except Exception as e:  # a bad item must not abort the whole pass
            logger.warning("backfill failed for %s: %s", url, e)
            result.errors += 1
        if progress is not None:
            progress(result)
        if sleep_s:
            time.sleep(sleep_s)
    return result
