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
from urllib.parse import urlsplit, urlunsplit

import httpx

from .sqlite_cache import (
    rows_needing_hash,
    rows_with_hash,
    set_content_hash,
    set_fetch_outcome,
)

logger = logging.getLogger(__name__)

# Generous, because it is a ceiling on honesty rather than on speed: below it the
# hash means "the whole document", and above it nothing is claimed at all.
HASH_MAX_BYTES = 5 * 1024 * 1024

class TooLarge(Exception):
    """The document exceeds the cap, so no honest whole-document hash exists.

    Carries `final_url` because this is raised mid-stream, after the redirect
    chain has already been resolved -- so it is one of the places that KNOWS
    which host served the oversized document, and the only place that can say so.
    """

    def __init__(self, message: str, *, final_url: str) -> None:
        super().__init__(message)
        self.final_url = final_url


@dataclass(frozen=True)
class PageRead:
    """What a fetch produced: the digest, AND the URL that actually answered.

    These travel together because they are one fact. When `hash_page` returned a
    bare digest, the address of the thing it had just read was discarded at this
    boundary, and every caller downstream had no choice but to assume the URL it
    requested was the URL that answered. It frequently is not: 177 rows in the
    live index recorded `blocked` against doi.org, which had in fact answered
    every one of them with a correct 302 -- the 403 came from the publisher it
    redirected to, whose name the index never saw.

    Returning a bare string again is the whole bug, so there is deliberately no
    way to get one out of this module.
    """

    digest: str
    final_url: str


def responding_url(exc: BaseException, requested: str) -> str:
    """Which URL produced this failure -- not necessarily the one we asked for.

    Falls back to the requested URL only when the exception genuinely carries no
    address, which is honest: a DNS failure that never reached a server has no
    responding host, and claiming otherwise would invent one.

    `request` and `response` are read through try/except rather than `getattr`
    with a default, because on httpx they are PROPERTIES that raise RuntimeError
    when unset -- and `getattr(exc, "request", None)` swallows only
    AttributeError, so the RuntimeError would escape from inside the error
    handler and turn a routine dead link into a crash.
    """
    final = getattr(exc, "final_url", "")
    if final:
        return str(final)
    for attribute in ("response", "request"):
        try:
            carrier = getattr(exc, attribute)
        except Exception:
            continue
        url = getattr(carrier, "url", None)
        if url:
            return str(url)
    return requested


def page_is_visible(url: str, *, client: httpx.Client) -> bool:
    """Can an anonymous reader see this URL at all? Not whether it hashes.

    Sends the SAME User-Agent as `hash_page`, and now cannot do otherwise: the
    header is a default on the client both are handed, not a convention each
    remembers. This answer is only meaningful as a comparison against the fetch
    it corroborates, and a host that varies its response by agent would make a
    probe under a different name compare two things that were never alike.

    Anything that is not a clean 404/410 counts as visible: the question is
    whether we SAW it, and a timeout or a 403 did not tell us it was absent.
    """
    try:
        response = client.get(url)
    except Exception:
        return False
    return response.status_code not in (404, 410)


def hash_page(url: str, *, client: httpx.Client) -> PageRead:
    """sha256 of the complete response body, and the URL that served it.

    Streamed, so an enormous document is abandoned at the cap instead of being
    read into memory. The client is the SSRF-guarded one the title fetcher
    builds: this walks stored URLs unattended, which is exactly where a redirect
    into a private address would go unnoticed.

    `resp.url` is read BEFORE `raise_for_status`, so the address is in hand on
    every path out of here rather than only the successful one.
    """
    digest = hashlib.sha256()
    read = 0
    with client.stream("GET", url) as resp:
        final_url = str(resp.url)
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            read += len(chunk)
            if read > HASH_MAX_BYTES:
                raise TooLarge(
                    f"{url} exceeds {HASH_MAX_BYTES} bytes", final_url=final_url
                )
            digest.update(chunk)
    return PageRead(digest=digest.hexdigest(), final_url=final_url)


# What the last read of a page ended as. Stored per row, because "could not
# read it" was one word covering two findings that mean opposite things.
OK = "ok"
GONE = "gone"                    # 404/410 -- the citation no longer resolves
BLOCKED = "blocked"              # 401/403 -- refused; the page may be perfectly fine
RATE_LIMITED = "rate_limited"    # 429 -- back off, conclude nothing
SERVER_ERROR = "server_error"    # 5xx -- their fault, probably transient
TIMEOUT = "timeout"
TOO_LARGE = "too_large"          # read fine, refused deliberately at the cap
UNREACHABLE = "unreachable"      # DNS, connection, and anything unrecognised
# A 404 we are NOT entitled to read as absence. See `absence_is_corroborated`.
NOT_VISIBLE = "not_visible"      # 404/410 whose whole container is also hidden


def parent_url(url: str) -> str | None:
    """The container one level up, or None at the root of a site.

    Used to ask whether a 404 sits inside a subtree we cannot see at all.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if "/" not in path.strip("/"):
        # One segment or none: the parent is the site root, and a site root that
        # answers is no evidence about a missing page beneath it -- github.com/
        # is up for everybody.
        return None
    return urlunsplit((parts.scheme, parts.netloc, path.rsplit("/", 1)[0], "", ""))


def absence_is_corroborated(url: str, *, visible: Callable[[str], bool]) -> bool:
    """Whether a 404 here is evidence the page is GONE, or only that we cannot see it.

    A status code is a fact about THIS REQUESTER's view, not about the resource.
    We fetch with no credentials, so a 404 confounds "no longer there" with
    "there, and not visible to you" -- and hosts deliberately answer the second
    with the first. GitHub does exactly this for private repositories, so it does
    not leak which ones exist; 219 rows in the live index recorded `gone` for the
    owner's own private pull requests, every one of which returns 200 and a real
    title to an authenticated request.

    The discriminator is CONTAINMENT, and it needs no credentials and one extra
    request. If the immediate parent is also invisible, the whole subtree is
    hidden from us and absence cannot be claimed. If the parent answers and only
    the leaf is missing, the leaf really is missing. Measured on both:

        private repo   /musharna/orchid-sdxl/pull/3  404, parent /pull  404
        public repo    /musharna/ghostcite/pull/99999 404, parent /pull 200

    The IMMEDIATE parent, not the topmost reachable ancestor: in the private case
    `/musharna` answers 200 (a user profile is public), so walking to the top
    would have called it corroborated and re-made the same false claim.

    A URL with no parent keeps `gone`. There is nothing left to ask, and refusing
    to ever say `gone` for a site root would throw away the real finding to avoid
    a rarer one.
    """
    parent = parent_url(url)
    if parent is None:
        return True
    return visible(parent)


def classify_failure(exc: BaseException) -> str:
    """What kind of failure this was, in one word the index can store.

    Only 404 and 410 may become GONE. Anything unrecognised falls to UNREACHABLE
    instead, because guessing "gone" from an error we do not understand would
    manufacture link rot -- inventing the exact finding this tool exists to
    report truthfully.
    """
    if isinstance(exc, TooLarge):
        return TOO_LARGE
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (404, 410):
            return GONE
        if code in (401, 403):
            return BLOCKED
        if code == 429:
            return RATE_LIMITED
        if 500 <= code < 600:
            return SERVER_ERROR
        return UNREACHABLE
    if isinstance(exc, httpx.TimeoutException):
        return TIMEOUT
    return UNREACHABLE


@dataclass
class SnapshotResult:
    examined: int = 0
    hashed: int = 0
    would_hash: int = 0
    unreachable: int = 0
    too_large: int = 0
    stamp_refused: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)
    # Keyed by the host that ANSWERED rather than the one the citation names.
    # Counting by requested host reported "doi.org: 177" -- a resolver that had
    # done its job correctly every time -- and never named the publishers doing
    # the refusing.
    #
    # TWO tallies, because a closed door and an empty room are the distinction
    # this whole subsystem exists to preserve, and the first version of this
    # report threw it away again one level up: it put 403s and 404s in one list
    # headed "who refused us", which ranked github.com first with 243 -- 241 of
    # them dead links that github had served perfectly correctly. They also call
    # for opposite actions (ask for access vs. repair or retire the citation).
    # `too_large` appears in NEITHER: that is us refusing, not the host.
    refused_by: dict[str, int] = field(default_factory=dict)   # 401/403, 429
    gone_at: dict[str, int] = field(default_factory=dict)      # 404/410
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
    hasher: Callable[[str], PageRead],
    clock: Callable[[], str],
    dry_run: bool = False,
    limit: int | None = None,
    include_failed: bool = False,
    only_outcome: str | None = None,
    only_host: str | None = None,
    sleep_s: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    visible: Callable[[str], bool] | None = None,
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
    for row in rows_needing_hash(
        db_path, limit=limit, include_failed=include_failed,
        only_outcome=only_outcome, only_host=only_host,
    ):
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
            read = hasher(url)
            read_at = clock()
        except Exception as e:  # a dead link is the common case, not a fault
            outcome = classify_failure(e)
            # WHERE the refusal came from. For a bare host this is the URL we
            # asked for; through a resolver it is somebody else entirely, and
            # that somebody is the finding.
            answered_by = responding_url(e, url)
            # `gone` is the only outcome that makes a claim about the SOURCE
            # rather than about our attempt, so it is the only one that has to be
            # corroborated before it is written down. Without a prober the claim
            # degrades to the weaker one that is always true -- absence is what
            # we would be inventing, so the default must never assert it.
            if outcome == GONE and not absence_is_corroborated(
                answered_by,
                # No prober means nothing can be SEEN, which is not the same as
                # nothing to ask: a URL with no parent is decided without ever
                # consulting this, and short-circuiting on `visible is None`
                # downgraded those too.
                visible=visible if visible is not None else (lambda _u: False),
            ):
                outcome = NOT_VISIBLE
            result.by_outcome[outcome] = result.by_outcome.get(outcome, 0) + 1
            host = urlsplit(answered_by).netloc
            if host:
                # A refusal is something the host DID; an absence is something
                # about the citation. Same host, opposite findings, opposite
                # remedies -- so they are never added to the same tally.
                if outcome in (BLOCKED, RATE_LIMITED):
                    result.refused_by[host] = result.refused_by.get(host, 0) + 1
                elif outcome == GONE:
                    result.gone_at[host] = result.gone_at.get(host, 0) + 1
            if outcome == TOO_LARGE:
                logger.info(
                    "%s is larger than the hash cap; not recording a hash", url
                )
                result.too_large += 1
                result.too_large_urls.append(url)
            else:
                # The redirect is named only when there WAS one. Printing
                # "(via itself)" on every ordinary dead link would bury the
                # handful of lines where the distinction is the whole point.
                if answered_by != url:
                    logger.info(
                        "could not read %s (%s): refused by %s: %s",
                        url,
                        outcome,
                        answered_by,
                        e,
                    )
                else:
                    logger.info("could not read %s (%s): %s", url, outcome, e)
                result.unreachable += 1
                result.unreachable_urls.append(url)
            # The ATTEMPT is stamped even though the read is not. `hashed_at`
            # stays empty because nothing was read; `last_attempt_at` records
            # that we tried, which is what stops the next pass repeating it.
            set_fetch_outcome(
                db_path, url, outcome=outcome, at=clock(), final_url=answered_by
            )
            continue

        digest = read.digest
        result.by_outcome[OK] = result.by_outcome.get(OK, 0) + 1
        set_fetch_outcome(
            db_path, url, outcome=OK, at=read_at, final_url=read.final_url
        )

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
    hasher: Callable[[str], PageRead],
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
            digest = hasher(url).digest
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
    # "unreachable: 1285" hides that most of it was a closed door rather than a
    # dead citation. The split is the whole point of recording an outcome.
    failures = {k: v for k, v in result.by_outcome.items() if k != OK}
    if failures:
        lines.append("why they could not be read:")
        for outcome, n in sorted(failures.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {outcome:<14}: {n}")
    # Attributed to the host that ANSWERED. The same tally keyed by the host the
    # citation names put "doi.org" at the top with 177, which named a resolver
    # that had answered correctly every time and named none of the publishers
    # actually refusing us.
    for title, tally in (
        ("who refused us (401/403/429, after redirects)", result.refused_by),
        ("where the dead links are (404/410, after redirects)", result.gone_at),
    ):
        if not tally:
            continue
        lines.append(f"{title}:")
        for host, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {host:<32}: {n}")
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
