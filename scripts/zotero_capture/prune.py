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

from .opjournal import OperationJournal
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
    # Selected from the snapshot, but no longer the item that was selected.
    skipped: int = 0
    # Split by OUTCOME, not by selection. There used to be one `urls` list,
    # appended before the trash was attempted, and the CLI printed all of it as
    # "trashed: <url>" -- so an item the CAS guard refused was itemised as
    # removed above a summary that counted zero. One ambiguous list read by a
    # consumer that could only guess is the whole defect; there is no longer a
    # list that means "selected, outcome unknown" for anyone to misread.
    selected: list[str] = field(default_factory=list)
    trashed_urls: list[str] = field(default_factory=list)
    skipped_urls: list[str] = field(default_factory=list)
    error_urls: list[str] = field(default_factory=list)


def prune(
    zotero: ZoteroClient,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    sleep_s: float = DEFAULT_SLEEP_S,
    progress: object = None,
    journal: OperationJournal | None = None,
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
        result.selected.append(url)
        if dry_run:
            result.would_trash += 1
            continue
        # Journalled BEFORE the write. The items land in Zotero's trash and a
        # human can get them back, but "which of these did that run put here"
        # was unanswerable — and that is the question you have when a pass
        # surprises you.
        seq = (
            journal.step(target=key, action="trash", before={"url": url})
            if journal
            else 0
        )
        try:
            # The snapshot url is the evidence this item was chosen on. The walk
            # completes before any write, so minutes can pass in between.
            if not zotero.trash_item(key, expect_url=url):
                if journal:
                    journal.outcome(seq, "refused", "item moved since selection")
                result.skipped += 1
                result.skipped_urls.append(url)
                continue
            if journal:
                journal.outcome(seq, "done")
            result.trashed += 1
            result.trashed_urls.append(url)
        except Exception as e:  # one bad item must not abort the pass
            if journal:
                journal.outcome(seq, "failed", str(e))
            logger.warning("prune failed for %s: %s", url, e)
            result.errors += 1
            result.error_urls.append(url)
        if sleep_s:
            time.sleep(sleep_s)
    return result


def format_prune_report(result: PruneResult, *, dry_run: bool) -> list[str]:
    """The lines a human reads, each URL under the outcome it actually had.

    Rendering lives here rather than in the CLI because the CLI is where the
    misreport happened: it held its own idea of what the result meant, and that
    idea was wrong. A refusal is named rather than counted in silence -- the CAS
    guard exists to stop a destructive write against an item that moved, and an
    operator who is not told it fired has no way to go look at what changed.
    """
    lines: list[str] = []
    if dry_run:
        lines += [f"  would trash: {url}" for url in result.selected]
    else:
        lines += [f"  trashed: {url}" for url in result.trashed_urls]
        lines += [
            f"  refused: {url}  (moved since it was selected)"
            for url in result.skipped_urls
        ]
        lines += [f"  error:   {url}" for url in result.error_urls]

    verb = "would trash" if dry_run else "trashed"
    count = result.would_trash if dry_run else result.trashed
    lines.append(f"examined  : {result.examined}")
    lines.append(f"{verb:<10}: {count}")
    if not dry_run:
        lines.append(f"refused   : {result.skipped}")
    lines.append(f"errors    : {result.errors}")
    if not dry_run and count:
        lines.append("")
        lines.append(
            "These are in the Zotero trash, not deleted. Restore from any client."
        )
    return lines
