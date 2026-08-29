"""Record what a page said when it was read, so a change becomes detectable.

A captured item is a URL and a title. If the page is edited, paywalled or taken
down, nothing in the library can show what was actually consulted — which is the
one thing a provenance record exists to do. Storing a hash does not preserve the
content, but it turns "this citation might have said anything" into "this
citation no longer says what it said", and that is the difference between a
reference you can defend and one you can only hope about.

This runs UNATTENDED, never from the Stop hook. `fetch_title` deliberately stops
reading at `</title>`, so there is no full body lying around to hash for free;
getting one means reading the whole document, and the hook's budget is the
reason the title fetch has a one-second cap in the first place. Hashing belongs
where the title backfill already lives.

**The hash covers the COMPLETE document or it is not recorded.** A document
larger than `HASH_MAX_BYTES` is skipped and reported, rather than hashed to its
first few megabytes. A hash that silently means "a prefix of the page" would
compare equal for two documents that differ after the cap, which is a false
negative in exactly the case — a long page quietly edited near the end — that
the hash is there to catch.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .sqlite_cache import rows_needing_hash, rows_with_hash, set_content_hash

logger = logging.getLogger(__name__)

# Generous, because it is a ceiling on honesty rather than on speed: below it the
# hash means "the whole document", and above it nothing is claimed at all.
HASH_MAX_BYTES = 5 * 1024 * 1024

_USER_AGENT = "zotero-provenance"


class TooLarge(Exception):
    """The document exceeds the cap, so no honest whole-document hash exists."""


def hash_page(url: str, *, client: httpx.Client) -> str:
    """sha256 of the complete response body.

    Streamed, so an enormous document is abandoned at the cap instead of being
    read into memory. The client is the SSRF-guarded one the title fetcher
    builds: this walks stored URLs unattended, which is exactly where a redirect
    into a private address would go unnoticed.
    """
    digest = hashlib.sha256()
    read = 0
    with client.stream("GET", url, headers={"User-Agent": _USER_AGENT}) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            read += len(chunk)
            if read > HASH_MAX_BYTES:
                raise TooLarge(f"{url} exceeds {HASH_MAX_BYTES} bytes")
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class SnapshotResult:
    examined: int = 0
    hashed: int = 0
    would_hash: int = 0
    unreachable: int = 0
    too_large: int = 0
    stamp_refused: int = 0
    # Split by outcome, per the lesson prune paid for.
    hashed_urls: list[str] = field(default_factory=list)
    unreachable_urls: list[str] = field(default_factory=list)
    too_large_urls: list[str] = field(default_factory=list)
    stamp_refused_urls: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    examined: int = 0
    unchanged: int = 0
    changed: int = 0
    unreachable: int = 0
    changed_urls: list[str] = field(default_factory=list)
    unreachable_urls: list[str] = field(default_factory=list)


def snapshot(
    db_path,
    *,
    zotero,
    hasher: Callable[[str], str],
    clock: Callable[[], str],
    dry_run: bool = False,
    limit: int | None = None,
    sleep_s: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SnapshotResult:
    """Hash every completed row that has never been hashed.

    `clock` is read once per page rather than once per run, because `hashed_at`
    records WHEN THE PAGE WAS READ and that is a fact about the fetch, not about
    the batch it happened to be in. A single timestamp threaded through the whole
    loop was wrong by up to the length of the run: a full pass over this corpus
    takes hours, so a page read at the end was stamped with the hour it started.
    A provenance timestamp that is confidently wrong is worse than a coarse one,
    because nothing downstream can tell.

    It is sampled immediately after the fetch returns, not after the Zotero
    stamp: the stamp is a separate round trip and its latency is not part of when
    the page was read.
    """
    result = SnapshotResult()
    last_request: dict[str, float] = {}
    for row in rows_needing_hash(db_path, limit=limit):
        url = row["url_canonical"]
        result.examined += 1
        if dry_run:
            result.would_hash += 1
            continue

        if sleep_s:
            host = urlsplit(url).netloc
            previous = last_request.get(host)
            if previous is not None:
                remaining = sleep_s - (monotonic() - previous)
                if remaining > 0:
                    sleeper(remaining)
            # Stamped before the fetch rather than after, so the interval runs
            # between request STARTS and the time the host already spent
            # serving us counts toward it. Stamped unconditionally for the same
            # reason: a dead link and an oversized page are requests the host
            # answered, and on an old corpus a long run of failures is the
            # likeliest way to end up sprinting through one site.
            last_request[host] = monotonic()
        try:
            digest = hasher(url)
            read_at = clock()
        except TooLarge:
            logger.info("%s is larger than the hash cap; not recording a hash", url)
            result.too_large += 1
            result.too_large_urls.append(url)
            continue
        except Exception as e:  # a dead link is the common case, not a fault
            logger.info("could not read %s: %s", url, e)
            result.unreachable += 1
            result.unreachable_urls.append(url)
            continue

        # The index is written only after the item is stamped. Reversed, a
        # refused stamp would leave the index claiming a hash that appears
        # nowhere in the library, and the next pass would skip the row for
        # having one.
        if not zotero.record_content_hash(row["zotero_key"], digest, expect_url=url):
            result.stamp_refused += 1
            result.stamp_refused_urls.append(url)
            continue
        set_content_hash(db_path, url, content_hash=digest, hashed_at=read_at)
        result.hashed += 1
        result.hashed_urls.append(url)
    return result


def verify(
    db_path,
    *,
    hasher: Callable[[str], str],
    limit: int | None = None,
) -> VerifyResult:
    """Ask whether each hashed page still says what it said.

    Read-only against both the library and the index. A page that has changed is
    NOT re-hashed here: the stored hash is the evidence of what was consulted,
    and quietly replacing it with what the page says today would destroy the
    finding at the moment it was made.
    """
    result = VerifyResult()
    for row in rows_with_hash(db_path, limit=limit):
        url = row["url_canonical"]
        result.examined += 1
        try:
            digest = hasher(url)
        except Exception as e:
            logger.info("could not re-read %s: %s", url, e)
            result.unreachable += 1
            result.unreachable_urls.append(url)
            continue
        if digest == row["content_hash"]:
            result.unchanged += 1
        else:
            result.changed += 1
            result.changed_urls.append(url)
    return result


def format_snapshot_report(result: SnapshotResult, *, dry_run: bool) -> list[str]:
    lines: list[str] = []
    if dry_run:
        lines.append(f"would hash    : {result.would_hash}")
        lines.append(f"examined      : {result.examined}")
        return lines
    lines += [f"  hashed: {url}" for url in result.hashed_urls]
    lines += [f"  unreachable: {url}" for url in result.unreachable_urls]
    lines += [
        f"  too large: {url}  (no whole-document hash)" for url in result.too_large_urls
    ]
    lines += [
        f"  stamp refused: {url}  (item moved)" for url in result.stamp_refused_urls
    ]
    lines.append(f"examined      : {result.examined}")
    lines.append(f"hashed        : {result.hashed}")
    lines.append(f"unreachable   : {result.unreachable}")
    lines.append(f"too large     : {result.too_large}")
    lines.append(f"stamp refused : {result.stamp_refused}")
    return lines


def format_verify_report(result: VerifyResult) -> list[str]:
    lines = [f"  CHANGED: {url}" for url in result.changed_urls]
    lines += [f"  unreachable: {url}" for url in result.unreachable_urls]
    lines.append(f"examined      : {result.examined}")
    lines.append(f"unchanged     : {result.unchanged}")
    lines.append(f"CHANGED       : {result.changed}")
    lines.append(f"unreachable   : {result.unreachable}")
    if result.changed:
        lines.append("")
        lines.append(
            "A changed page still carries its ORIGINAL hash: that is the record "
            "of what was consulted, and it is not overwritten with what the page "
            "says today."
        )
    return lines
