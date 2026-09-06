"""Work the retry queue, so recovery does not depend on a re-citation.

Until now a failed Zotero write was logged and dropped. Both recovery paths in
capture need the URL to be cited AGAIN -- an unissued claim is released so a
later run can retry it, and an issued one is settled by `_resolve_claim` on the
next citation -- so a source cited once, which failed once, was gone. For a tool
whose whole job is to record what was actually consulted, that is the worst kind
of loss: silent, and invisible to the person relying on the library.

The README said the queue was inert "precisely so a queue nothing drains cannot
grow forever". That reasoning was right, and it is why this ships WITH its
drain and with two bounds rather than as a queue alone.

A drain never issues a write of its own. It replays the URL through
`capture_message`, the same function the hook calls, so it inherits the
reservation, the claim resolution and the dedup that already exist to answer
"did that POST commit?" -- and the original sighting date, so a recovered source
is filed under the day it was cited rather than the day the retry ran.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .sqlite_cache import (
    RETRY_MAX_ATTEMPTS,
    RetryEntry,
    dequeue_retry,
    retry_queue_entries,
)

logger = logging.getLogger(__name__)


@dataclass
class DrainResult:
    examined: int = 0
    recovered: int = 0
    still_failing: int = 0
    abandoned: int = 0
    would_retry: int = 0
    # Split by outcome. A single list of "everything the drain touched" is the
    # shape that let prune report refused items as trashed.
    recovered_urls: list[str] = field(default_factory=list)
    still_failing_urls: list[str] = field(default_factory=list)
    abandoned_urls: list[str] = field(default_factory=list)


def drain(
    db_path,
    *,
    replay: Callable[[RetryEntry], object],
    dry_run: bool = False,
    limit: int | None = None,
) -> DrainResult:
    """Retry each queued URL, oldest failure first.

    `replay` is injected rather than built here so this stays testable without a
    Zotero client, and so the caller owns the credentials.

    An entry that has already been attempted `RETRY_MAX_ATTEMPTS` times is given
    up on and removed. Retrying forever is the failure mode a bounded queue
    exists to prevent, and an entry that has failed five times is not going to
    succeed on the sixth because a person ran the command again -- it needs a
    human to look at why. Abandoning is reported, never silent.
    """
    result = DrainResult()
    for entry in retry_queue_entries(db_path, limit=limit):
        result.examined += 1
        url = entry["url_canonical"]

        if entry["attempts"] >= RETRY_MAX_ATTEMPTS:
            if dry_run:
                result.abandoned += 1
                result.abandoned_urls.append(url)
                continue
            logger.warning(
                "giving up on %s after %d attempts; last error: %s",
                url,
                entry["attempts"],
                entry["last_error"],
            )
            dequeue_retry(db_path, url)
            result.abandoned += 1
            result.abandoned_urls.append(url)
            continue

        if dry_run:
            result.would_retry += 1
            continue

        try:
            outcome = replay(entry)
        except Exception as e:  # one bad entry must not abort the pass
            logger.warning("retry raised for %s: %s", url, e)
            result.still_failing += 1
            result.still_failing_urls.append(url)
            continue

        # capture_message re-queues its own failures, incrementing the attempt
        # count as it goes, so a still-failing entry needs nothing done to it
        # here. Only a clean run earns a dequeue.
        errors = getattr(outcome, "errors", None) or []
        deferred = getattr(outcome, "urls_deferred", None) or []
        if any(getattr(e, "url", None) == url for e in errors) or url in deferred:
            # A deferral is not a failure and not a recovery: the claim is
            # inside its window and will be settled by a later pass. Leaving
            # the entry alone (no attempt counted) is the only honest move.
            result.still_failing += 1
            result.still_failing_urls.append(url)
            continue

        dequeue_retry(db_path, url)
        result.recovered += 1
        result.recovered_urls.append(url)
    return result


def format_drain_report(result: DrainResult, *, dry_run: bool) -> list[str]:
    """Each URL under the outcome it actually had."""
    lines: list[str] = []
    if dry_run:
        lines.append(f"  would retry: {result.would_retry}")
        lines += [f"  would abandon: {url}" for url in result.abandoned_urls]
    else:
        lines += [f"  recovered: {url}" for url in result.recovered_urls]
        lines += [f"  still failing: {url}" for url in result.still_failing_urls]
        lines += [
            f"  abandoned: {url}  (after {RETRY_MAX_ATTEMPTS} attempts)"
            for url in result.abandoned_urls
        ]

    lines.append(f"examined      : {result.examined}")
    if dry_run:
        lines.append(f"would retry   : {result.would_retry}")
    else:
        lines.append(f"recovered     : {result.recovered}")
        lines.append(f"still failing : {result.still_failing}")
    lines.append(f"abandoned     : {result.abandoned}")
    if result.abandoned:
        lines.append("")
        lines.append(
            "Abandoned entries are no longer queued. They failed "
            f"{RETRY_MAX_ATTEMPTS} times; the last error for each is in the log."
        )
    return lines
