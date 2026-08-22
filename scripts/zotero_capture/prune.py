"""Remove items the exclusion rules say should never have been captured.

`is_excluded` is the single definition of what is not a source. Capture consults
it going forward; this walks the items already in the collection and applies the
same predicate, so a rule added later can be swept back over the backlog without
a second, drifting list of what counts as junk.

Items are moved to the Zotero trash, not deleted. The collection is a provenance
record, so a cleanup pass has to be reversible by a human who disagrees with it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .url_processing import is_excluded
from .zotero_client import ZoteroClient

logger = logging.getLogger(__name__)

DEFAULT_SLEEP_S = 0.4


@dataclass
class PruneResult:
    examined: int = 0
    trashed: int = 0
    would_trash: int = 0
    errors: int = 0
    urls: list[str] = field(default_factory=list)


def prune(
    zotero: ZoteroClient,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    sleep_s: float = DEFAULT_SLEEP_S,
    progress: object = None,
) -> PruneResult:
    """Trash every item in the collection whose URL the exclusion rules reject.

    The walk is completed BEFORE anything is written. Zotero excludes trashed
    items from ordinary listings, and the walk pages by offset, so trashing as
    we went shifted every later entry left and the next `start=` stepped over
    exactly as many items as had just been removed. The sweep then reported a
    clean pass over a collection it had only partly seen.

    Holding the collection costs one (key, url) pair per item, which is small
    next to the alternative of silently skipping some of them.
    """
    result = PruneResult()
    snapshot: list[tuple[str, str]] = []
    for item in zotero.iter_collection_items():
        if limit is not None and len(snapshot) >= limit:
            break
        url = item.get("data", {}).get("url") or ""
        if not url:
            continue
        snapshot.append((item["key"], url))

    for key, url in snapshot:
        result.examined += 1
        if not is_excluded(url):
            continue
        result.urls.append(url)
        if dry_run:
            result.would_trash += 1
            continue
        try:
            zotero.trash_item(key)
            result.trashed += 1
        except Exception as e:  # one bad item must not abort the pass
            logger.warning("prune failed for %s: %s", url, e)
            result.errors += 1
        if sleep_s:
            time.sleep(sleep_s)
    return result
