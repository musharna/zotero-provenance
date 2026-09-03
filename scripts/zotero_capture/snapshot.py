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

**A hash always states what it covers.** A document that fits under `max_bytes`
is hashed whole; one that does not is hashed to exactly its first `max_bytes`
bytes and RECORDED AS SUCH. The scope travels with the digest, the way HTTP's
own `Repr-Digest` travels with a `Content-Range`.

It did not use to. `content_hash` was a single column with no room to say what
it covered, so a prefix stored there would have been read as a whole-document
claim — and would compare equal for two documents differing after the cap, a
false negative in exactly the case the hash exists to catch. Given that column
the all-or-nothing rule was forced rather than chosen, and the price was that
the 71 largest sources in the corpus — 39 arXiv PDFs, a Nature paper, an SEC
filing, several genome assemblies — were fetched successfully and held no
evidence at all. Raising the cap could never have fixed that: the largest is a
207 GiB archive, so there is no ceiling that reaches the tail.

A prefix hash is ONE-DIRECTIONAL, and every consumer must treat it so. A
difference inside the covered range proves the document changed; agreement
proves nothing whatever about the bytes past the cap. `verify` therefore cannot
report a truncated row as "unchanged" — the same discipline that stops a 404
becoming "gone".
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, NamedTuple
from urllib.parse import urlsplit, urlunsplit

import httpx

from .url_processing import (
    HEAD_SNIFF_BYTES,
    document_disowns,
    is_vcs_requirement,
    unbalanced_brackets,
)
from .sqlite_cache import (
    rows_needing_hash,
    rows_with_hash,
    set_content_hash,
    set_fetch_outcome,
    set_stable_digest,
    set_verify_outcome,
    unverified_count,
)

logger = logging.getLogger(__name__)

# How much of a document we are willing to pull, per page. This is now purely a
# COST bound: below it the digest covers everything, above it the digest covers
# a stated prefix, and either way something is recorded. While a partial read was
# inexpressible this number decided whether evidence existed at all, and 5 MiB
# left the corpus's 71 biggest sources with none -- the median of them is 11.8
# MiB, so most are ordinary papers that simply did not fit.
#
# It buys completeness rather than honesty now, so it is set where completeness
# actually lands: 32 MiB covers three quarters of the oversized rows outright.
# It does NOT chase the tail, and nothing should -- the largest single citation
# in the corpus is a 207 GiB archive.
HASH_MAX_BYTES = 32 * 1024 * 1024


class UnitRead(NamedTuple):
    """One unit of a document, as a digest and a length.

    The text is deliberately not kept. `hash_page` streams precisely so an
    enormous document is never held in memory, and a stable digest that
    required the body would have quietly undone that.
    """

    digest: str
    nbytes: int


class StableRead(NamedTuple):
    """A digest over the units two reads agreed on, and the bytes it covers.

    The scope travels with the digest for the same reason `PageRead` carries
    `covers_bytes`: a digest that cannot say what it covers can only be
    all-or-nothing, and this one covers a subset by construction.
    """

    digest: str
    covers_bytes: int


# The byte table the rolling hash is built from. Derived from sha256 rather
# than from a seeded PRNG because this table DEFINES where documents are cut:
# a table that depended on an interpreter's random implementation would re-cut
# every document on an upgrade and report every source in the library as having
# changed. It must never be edited for the same reason -- see STABLE_ALGO.
_GEAR = tuple(
    int.from_bytes(hashlib.sha256(bytes([b])).digest()[:8], "big") for b in range(256)
)
_MASK64 = (1 << 64) - 1

# Where a document is cut into units. A boundary falls wherever the rolling
# hash of the preceding bytes hits the mask, so boundaries follow CONTENT.
#
# That property is the whole point, and it is not merely "smaller units".
# Measured 2026-09-03 against fixed-size 512-byte blocks as a control: an edit
# that changes a document's LENGTH shifts every byte after it, and fixed blocks
# never recover -- one page went from 0.9966 coverage to 0.1881, another scored
# 0.2885 where content-defined chunks scored 0.9188. A content-defined boundary
# re-synchronises at the next hash hit, so an edit costs only the unit holding
# it.
#
# 64 bytes, not 512, and not 16: measured over 38 live pairs, scored against
# what actually differs between two reads. At 512 bytes 12 characterisable
# documents were still refused; at 64 bytes none were, with no page wrongly
# accepted. Below that the units stop being evidence of shared CONTENT and
# start matching boilerplate by coincidence.
CHUNK_MASK = 0b11_1111
CHUNK_MIN = 16
CHUNK_MAX = 1024

# A document with more units than this records none. Bounded because the point
# of streaming is that page size cannot become memory, and a million-unit
# document would put it straight back. At ~64 bytes a unit this stops at about
# 12.8 MB; every row in the live index above that is a .zip, .h5ad, PDF or
# release binary, for which no chunking scheme means anything.
MAX_UNIT_DIGESTS = 200_000

# Which method produced a stored stable digest. A digest is comparable ONLY
# with one made the same way, so this travels with it: when the stored tag is
# not this one the row is re-baselined, never reported as the source having
# changed. Without it, changing the unit -- as 0.53.0 did, from the line to the
# content-defined chunk -- would have made every stored digest mismatch at
# once, and the tool would have announced that 1,217 sources had drifted when
# the only thing that moved was us.
STABLE_ALGO = "cdc64/1"

# How much of the document the agreed units must cover before the digest is
# worth anything. Not a hedge, and NOT the knob that fixes coverage: measured
# 2026-09-03 over 38 live read-pairs, everything that is characterisable at all
# lands at 0.92 or above and everything genuinely volatile at 0.81 or below --
# a yahoo news page rebuilt 22% of itself between two reads three seconds
# apart. The floor sits in that gap and did not move when the unit changed,
# which is the evidence that the unit was the defect and the threshold was not.
MIN_STABLE_COVERAGE = 0.90


class _ChunkDigester:
    """Per-unit digests accumulated as the body streams past.

    The unit is a content-defined chunk, not a line. A line's length is chosen
    by whoever formatted the source, and comparison forfeits a whole unit for a
    single differing byte inside it -- so with lines, a source's formatter
    decided how much one nonce could cost us. Measured 2026-09-03 on the live
    index: springer stamps a per-render id into every reference anchor, ~9
    bytes each, and because those anchors sit inside one 105,082-byte line the
    document read as 30% volatile when 0.12% of it had actually changed. A
    630 KB huggingface page is a SINGLE line, so one token anywhere in it took
    the whole document to 0.65 coverage and no digest was ever stored.

    Cutting on a rolling hash of the content bounds that cost at CHUNK_MAX and
    takes the choice away from the source entirely.

    Transport chunk boundaries belong to the transport. `hash_page` already
    refuses to let them influence the byte cap -- "a digest that depended on
    them would differ for a document that had not" -- and the same trap sits
    here: the rolling hash carries ACROSS an `iter_bytes` boundary, so a body
    delivered in 1 KB pieces and the same body delivered in one piece cut
    identically. That is asserted directly rather than assumed.

    Overflowing `max_units` records NOTHING rather than a prefix. A partial list
    aligned against a full one would report the whole tail as volatile, which is
    a manufactured finding of exactly the kind a truncated hash produces.
    """

    def __init__(self, max_units: int = MAX_UNIT_DIGESTS) -> None:
        self._units: list[UnitRead] = []
        self._buf = bytearray()
        self._max = max_units
        self._overflowed = False
        self._rolling = 0
        # How far into the current unit the rolling hash has been fed. Starts
        # at CHUNK_MIN because a unit shorter than that is not allowed to end:
        # without a floor, a run of bytes that happens to hit the mask often
        # would shatter into units too short to be evidence of anything.
        self._scanned = CHUNK_MIN

    def update(self, chunk: bytes) -> None:
        if self._overflowed:
            return
        self._buf.extend(chunk)
        self._cut(final=False)

    def _cut(self, *, final: bool) -> None:
        buf = self._buf
        while not self._overflowed:
            n = len(buf)
            limit = min(CHUNK_MAX, n)
            i = self._scanned
            cut = -1
            while i < limit:
                self._rolling = ((self._rolling << 1) + _GEAR[buf[i]]) & _MASK64
                if not self._rolling & CHUNK_MASK:
                    cut = i + 1
                    break
                i += 1
            if cut < 0:
                if n >= CHUNK_MAX:
                    # No boundary in reach. Cut anyway, so one long run of
                    # bytes cannot become one enormous unit -- which is the
                    # defect this class exists to remove.
                    cut = CHUNK_MAX
                elif final and n:
                    cut = n
                else:
                    # Everything in hand is scanned; wait for more bytes rather
                    # than cutting at a boundary the transport chose.
                    self._scanned = i
                    return
            self._add(bytes(buf[:cut]))
            del buf[:cut]
            self._rolling = 0
            self._scanned = CHUNK_MIN
            if not buf:
                return

    def _add(self, unit: bytes) -> None:
        if len(self._units) >= self._max:
            self._overflowed = True
            self._units = []
            return
        self._units.append(UnitRead(hashlib.sha256(unit).hexdigest(), len(unit)))

    def finish(self) -> tuple[UnitRead, ...] | None:
        """The units, or None for "not recorded" -- never an empty tuple for it.

        A document really can have no units. Returning `()` for both would put
        two findings in one value, which is the defect that cost this project a
        release each for `unreachable` and `gone`.
        """
        self._cut(final=True)
        return None if self._overflowed else tuple(self._units)


def stable_digest(first: PageRead, second: PageRead) -> StableRead | None:
    """A digest over what two reads of the same page agreed on, or None.

    The volatile bytes are DERIVED, never named. Whatever two reads seconds
    apart disagree on is per-request by definition -- a nonce, a CSRF field, a
    per-render element id, an A/B bucket -- and listing those names instead
    would be a guard that cannot fail on a token nobody has invented yet.

    None means "no answer", and every caller must carry it as `unstable` rather
    than as agreement. It happens when either read recorded no units, and when
    the agreed units cover too little of the document to characterise it -- a
    page that genuinely rebuilds itself between two reads, which over 38 live
    pairs was 5 of them: news pages and dashboards, 13% to 24% of their bytes
    actually different three seconds apart.
    """
    left, right = first.units, second.units
    if left is None or right is None:
        return None
    matcher = difflib.SequenceMatcher(
        None, [x.digest for x in left], [x.digest for x in right], autojunk=False
    )
    agreed: list[UnitRead] = []
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            agreed.extend(left[i1:i2])
    total = sum(x.nbytes for x in left)
    kept = sum(x.nbytes for x in agreed)
    if total <= 0 or kept / total < MIN_STABLE_COVERAGE:
        return None
    rolling = hashlib.sha256()
    for line in agreed:
        rolling.update(line.digest.encode())
    return StableRead(digest=rolling.hexdigest(), covers_bytes=kept)


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
    # How many bytes this digest covers, and whether that was all of them.
    # Neither is defaulted, for the reason `final_url` is not: a parameter that
    # CAN be omitted eventually is, invisibly from both ends -- this repository
    # shipped `--sleep` parsed and dropped for an entire release. A digest whose
    # scope defaulted to "complete" would lie by omission in the one direction
    # that matters, turning a prefix into a whole-document claim.
    covers_bytes: int
    complete: bool
    # Per-unit digests, or None for "not recorded". This one DOES default,
    # unlike `final_url` and `complete` above, and the difference is the
    # direction the omission fails in: a missing `complete` would turn a prefix
    # into a whole-document claim, while a missing `units` can only ever
    # withhold a conclusion and leave the row `unstable`. A default that can
    # only be more cautious is safe; one that can be less is not.
    units: tuple[UnitRead, ...] | None = None


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


class NotTheResource(Exception):
    """A successful response that is not the resource that was asked for.

    Raised, not returned, and deliberately so. The alternative -- a flag on
    `PageRead` that each caller checks -- is one rule kept in three places, and
    a caller that forgets it records a bot wall as a provenance baseline. As an
    exception it routes through `classify_failure`, which is already the single
    place a failure is given its name.

    Carries `final_url` under that exact name because `responding_url` reads it
    first, so the address recorded is the challenge's own, not the publisher we
    asked and which did nothing wrong.
    """

    def __init__(self, *, declared: str, requested: str) -> None:
        super().__init__(f"{requested} answered with a document declaring {declared}")
        self.final_url = declared
        self.requested = requested


def hash_page(
    url: str, *, client: httpx.Client, max_bytes: int = HASH_MAX_BYTES
) -> PageRead:
    """sha256 of the response body up to `max_bytes`, with the span it covers.

    Streamed, so an enormous document is stopped at the cap instead of being read
    into memory. The client is the SSRF-guarded one the title fetcher builds:
    this walks stored URLs unattended, which is exactly where a redirect into a
    private address would go unnoticed.

    The cut lands at `max_bytes` exactly, never at the end of whichever chunk
    happened to cross it. Chunk sizes belong to the transport and vary between
    runs, so a digest that depended on them would differ for a document that had
    not -- reporting our own transport as provenance drift.

    Reaching the cap is not an error and no longer raises. It is a successful
    read of a stated prefix, and saying so is the whole point: the alternative,
    for six releases, was that the biggest sources in the library had no record
    at all.

    `resp.url` is read BEFORE `raise_for_status`, so the address is in hand on
    every path out of here rather than only the successful one.
    """
    digest = hashlib.sha256()
    units = _ChunkDigester()
    head = bytearray()
    read = 0
    complete = True
    with client.stream("GET", url) as resp:
        final_url = str(resp.url)
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            if len(head) < HEAD_SNIFF_BYTES:
                head.extend(chunk[: HEAD_SNIFF_BYTES - len(head)])
            if read + len(chunk) > max_bytes:
                # A document of EXACTLY max_bytes never reaches here, so it is
                # correctly complete: the boundary is a cap, not a refusal to
                # hash anything large.
                kept = chunk[: max_bytes - read]
                digest.update(kept)
                units.update(kept)
                read = max_bytes
                complete = False
                break
            digest.update(chunk)
            units.update(chunk)
            read += len(chunk)
    # Asked AFTER the body is in hand and BEFORE anything is returned, so no
    # caller can be handed a digest of a page that said it was somebody else.
    # This is the missing member of the family that already holds
    # `unbalanced_brackets` ("did we even send the cited address?") and
    # `absence_is_corroborated` ("may we read this 404 as absence?"). Each asks
    # what we are ENTITLED to conclude before the confident label is written.
    declared = document_disowns(bytes(head), final_url)
    if declared:
        raise NotTheResource(declared=declared, requested=url)
    return PageRead(
        digest=digest.hexdigest(),
        final_url=final_url,
        covers_bytes=read,
        complete=complete,
        # Fed the SAME bytes the digest was, cap included, so the two can never
        # describe different spans of the document.
        units=units.finish(),
    )


# What the last read of a page ended as. Stored per row, because "could not
# read it" was one word covering two findings that mean opposite things.
OK = "ok"
GONE = "gone"                    # 404/410 -- the citation no longer resolves
BLOCKED = "blocked"              # 401/403 -- refused; the page may be perfectly fine
RATE_LIMITED = "rate_limited"    # 429 -- back off, conclude nothing
# Ceiling on a single host's self-imposed delay. Unbounded backoff over 818
# rows from one host is indistinguishable from a hang.
MAX_PENALTY_S = 120.0
SERVER_ERROR = "server_error"    # 5xx -- their fault, probably transient
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"      # DNS, connection, and anything unrecognised
# A 404 we are NOT entitled to read as absence. See `absence_is_corroborated`.
NOT_VISIBLE = "not_visible"      # 404/410 whose whole container is also hidden
# A failure on an address we ourselves damaged. The 404 is a fact about the
# string we stored, not about the source -- see tests/test_truncated_urls.py.
MALFORMED = "malformed"          # our record is a prefix of the cited address

# What a RE-READ concluded, as opposed to how a fetch ended. Disjoint from the
# outcomes above on purpose: both vocabularies land in `verify_outcome`, and a
# row that says "blocked" and a row that says "changed" are answering the same
# question -- what happened the last time we looked at this page.
UNCHANGED = "unchanged"          # complete digest, identical
PREFIX_AGREED = "prefix_agreed"  # agreed over a prefix; the tail was never compared
CHANGED = "changed"              # differed, and said so twice
UNSTABLE = "unstable"            # did not agree with ITSELF; says nothing about drift
# A page whose per-request bytes move but whose document does not. Deliberately
# NOT folded into the words above: "unchanged" is a claim about the whole
# response and these are claims about the document inside it, and one word
# covering both is how "unreachable" came to mean gone AND blocked.
STABLE_BASELINE = "stable_baseline"    # first stable digest recorded; nothing compared
STABLE_UNCHANGED = "stable_unchanged"  # volatile bytes moved, the document did not
STABLE_CHANGED = "stable_changed"      # the document itself differs from the recorded one
# The stored digest was cut by an older method, so nothing about the source can
# be concluded from it and a fresh baseline replaces it. A fact about US, kept
# apart from `stable_baseline` (which means the page had never been
# characterised) because a run that re-cut 2,400 rows and a run that
# characterised 2,400 new pages are not the same event.
STABLE_REBASELINED = "stable_rebaselined"


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
    if isinstance(exc, NotTheResource):
        # Reused rather than given a fifth word. BLOCKED already reads
        # "refused; the page may be perfectly fine", which is exactly what a
        # challenge page is. A new outcome meaning the same thing would be a
        # second copy of one rule, and this repository has shipped four of
        # those against zero missing guards.
        return BLOCKED
    if isinstance(exc, httpx.InvalidURL):
        # Not a fact about the source and not a bug in the fetch: the string we
        # STORED is not a usable address, which is exactly what MALFORMED means
        # here. It reached this branch as UNREACHABLE before, blaming a host we
        # never managed to ask.
        return MALFORMED
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


# The failures that are FACTS ABOUT THE FETCH rather than bugs of ours. ONE
# tuple, because this same isinstance check stood open-coded at all three fetch
# sites and adding a fourth kind of failure to two of the three is the defect
# class that has shipped five times in this repository. A caller cannot now
# recognise a different set from its neighbour, because there is only one set.
FETCH_FAULTS = (httpx.HTTPError, httpx.InvalidURL, NotTheResource)


@dataclass
class SnapshotResult:
    examined: int = 0
    hashed: int = 0
    would_hash: int = 0
    unreachable: int = 0
    # Rows whose digest covers a stated prefix rather than the whole document.
    # Counted separately because it is a different STRENGTH of evidence, not a
    # different kind: a partial row can prove a change and can never disprove one.
    partial: int = 0
    # Something that was not an HTTP failure at all. Kept apart from every other
    # bucket because it is a fault of OURS, and the one thing that must never
    # happen is our bug being written down as a finding about a citation.
    internal_errors: int = 0
    internal_error_urls: list[str] = field(default_factory=list)
    stamp_refused: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)
    # A page that does not read the same way twice. Counting these as CHANGED
    # was reporting our own measurement noise as provenance drift.
    unstable: int = 0
    unstable_urls: list[str] = field(default_factory=list)
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
    # An oversized document appears in NEITHER, and no longer appears anywhere
    # as a failure: the host served it correctly and we record what we read.
    refused_by: dict[str, int] = field(default_factory=dict)   # 401/403, 429
    gone_at: dict[str, int] = field(default_factory=dict)      # 404/410
    # Split by outcome, per the lesson prune paid for.
    hashed_urls: list[str] = field(default_factory=list)
    unreachable_urls: list[str] = field(default_factory=list)
    partial_urls: list[str] = field(default_factory=list)
    stamp_refused_urls: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    examined: int = 0
    unchanged: int = 0
    changed: int = 0
    unreachable: int = 0
    # The stored digest covers only a prefix, and that prefix still agrees. This
    # is NOT `unchanged`: the bytes past the cap were never compared, and a long
    # document edited near its end is precisely the case a hash exists to catch.
    # Reporting it as unchanged would manufacture a reassurance -- strictly worse
    # than the silence this release replaced, because nothing downstream could
    # tell it was hollow.
    partial_match: int = 0
    partial_match_urls: list[str] = field(default_factory=list)
    internal_errors: int = 0
    changed_urls: list[str] = field(default_factory=list)
    unreachable_urls: list[str] = field(default_factory=list)
    # "unreachable" alone re-creates exactly the conflation 0.36.0 removed from
    # snapshot: a 403 from a paywall and a 404 on a dead citation are not the
    # same finding, and on this corpus most of a verify pass is the former.
    by_outcome: dict[str, int] = field(default_factory=dict)
    # A page that does not read the same way twice. Counting these as CHANGED
    # was reporting our own measurement noise as provenance drift.
    unstable: int = 0
    unstable_urls: list[str] = field(default_factory=list)
    # Rows rescued from `unstable` by deriving the volatile units from the two
    # reads this pass already takes. Counted apart from `unchanged` because they
    # answer a narrower question -- the document agreed, the response did not --
    # and adding them together would overstate what was actually compared.
    stable_baseline: int = 0
    # Rows whose stored digest was cut by an older method and was therefore
    # replaced rather than compared. A fact about a release, not about any
    # source, and it is reported under its own name so a sweep cannot present
    # it as either drift or discovery.
    stable_rebaselined: int = 0
    stable_unchanged: int = 0
    stable_changed: int = 0
    stable_changed_urls: list[str] = field(default_factory=list)
    # How many hashed rows have still never been verified, counted AFTER this
    # chunk. A chunked sweep otherwise cannot say whether it is finished: each
    # run reports what it did and nothing about what remains, which is the same
    # unmeasurability that let 71 blank rows sit unnoticed for six releases.
    unverified_remaining: int = 0


class _HostPacer:
    """A minimum interval between two requests to the SAME host.

    Extracted rather than copied. It lived inline in `snapshot`, which is why
    `verify` -- the other pass that fetches pages -- simply had none: there was
    nothing to reuse, so the politeness was a property of one loop instead of a
    property of fetching. `--sleep` was accepted, documented as "be polite", and
    delivered to exactly one of the two callers.

    Spacing is per HOST, not per request: rows arrive ordered by first_seen, so
    one site tends to appear as a contiguous burst while consecutive rows from
    different hosts need no delay between them at all.
    """

    def __init__(
        self,
        sleep_s: float,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep_s = sleep_s
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._last: dict[str, float] = {}
        # Extra seconds this host has ASKED for, on top of the configured
        # interval. The configured interval is our guess about what a host
        # tolerates; a 429 is the host saying the guess is wrong, and it was
        # the one input this class never took.
        self._penalty: dict[str, float] = {}

    def interval_for(self, url: str) -> float:
        """The current interval for this host: the base plus anything it has
        asked for. Exposed so a report can say why a run slowed down."""
        return self._sleep_s + self._penalty.get(urlsplit(url).netloc, 0.0)

    def penalise(self, url: str, retry_after: float | None = None) -> None:
        """This host told us to slow down.

        `Retry-After` wins when the host states one: guessing an exponential
        when we have been given the number is worse than either. Otherwise the
        penalty doubles, from a floor of the base interval, so a host that keeps
        refusing keeps getting more room. Capped, because an unbounded penalty
        on a corpus with 818 rows from one host is indistinguishable from a hang.
        """
        host = urlsplit(url).netloc
        if not host:
            return
        if retry_after is not None:
            self._penalty[host] = min(retry_after, MAX_PENALTY_S)
            return
        current = self._penalty.get(host, 0.0)
        floor = self._sleep_s if self._sleep_s else 1.0
        self._penalty[host] = min(max(current * 2.0, floor), MAX_PENALTY_S)

    def relax(self, url: str) -> None:
        """The host answered normally, so give some of the penalty back.

        Without decay a single 429 taxes every remaining row on that host for
        the rest of a multi-hour run. Halving rather than clearing keeps a
        memory of the refusal, so a host that throttles intermittently does not
        get sprinted at again the moment one request succeeds.
        """
        host = urlsplit(url).netloc
        current = self._penalty.get(host)
        if not current:
            return
        nxt = current / 2.0
        if nxt < 0.5:
            self._penalty.pop(host, None)
        else:
            self._penalty[host] = nxt

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        interval = self._sleep_s + self._penalty.get(host, 0.0)
        if not interval:
            return
        previous = self._last.get(host)
        if previous is not None:
            remaining = interval - (self._monotonic() - previous)
            if remaining > 0:
                self._sleeper(remaining)
        # Stamped before the fetch rather than after, so the interval runs
        # between request STARTS and the time the host already spent serving us
        # counts toward it. Stamped unconditionally for the same reason: a dead
        # link and an oversized page are requests the host answered, and on an
        # old corpus a long run of failures is the likeliest way to end up
        # sprinting through one site.
        self._last[host] = self._monotonic()


def _retry_after(response: httpx.Response) -> float | None:
    """`Retry-After` in seconds, or None when absent or unparseable.

    Only the delta-seconds form is read. The HTTP-date form is legal and rare,
    and misreading one would produce a wait of decades; returning None falls
    back to the exponential, which is wrong in the safe direction.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _paced_fetch(
    pacer: _HostPacer,
    hasher: Callable[[str, int], PageRead],
    url: str,
    max_bytes: int,
) -> PageRead:
    """The only place a page is fetched.

    Waits for this host's interval, reads, and lets the host's answer change
    that interval. One function on purpose: there were three `pacer.wait()` +
    `hasher()` pairs across the two passes, and putting the backoff in each
    failure arm would have made one rule into three copies -- the defect class
    that has shipped five times in this repository. It also means a future
    fetching pass cannot bypass the pacing the way `verify` originally did.

    Only 429 and 503 slow us down. A 404 is an answer, not a complaint, and
    this corpus holds 1,285 of them.
    """
    pacer.wait(url)
    try:
        read = hasher(url, max_bytes)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (429, 503):
            pacer.penalise(url, _retry_after(e.response))
        raise
    pacer.relax(url)
    return read


def snapshot(
    db_path,
    *,
    zotero,
    hasher: Callable[[str, int], PageRead],
    clock: Callable[[], str],
    dry_run: bool = False,
    max_bytes: int = HASH_MAX_BYTES,
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
    pacer = _HostPacer(sleep_s, sleeper=sleeper, monotonic=monotonic)
    for row in rows_needing_hash(
        db_path, limit=limit, include_failed=include_failed,
        only_outcome=only_outcome, only_host=only_host,
    ):
        url = row["url_canonical"]
        result.examined += 1
        if dry_run:
            result.would_hash += 1
            continue

        try:
            read = _paced_fetch(pacer, hasher, url, max_bytes)
            read_at = clock()
        except Exception as e:  # a dead link is the common case, not a fault
            if not isinstance(e, FETCH_FAULTS):
                # Not a fact about the source, so nothing about the source may
                # be written. Found by changing the hasher's signature: the old
                # stubs raised TypeError, `except Exception` caught it, and 20
                # rows were stamped `unreachable` -- a bug of ours recorded in
                # the library as link rot, which is the precise harm this whole
                # tool exists to prevent.
                #
                # Not re-raised, because a pass over this corpus runs for hours
                # and one strange row must not discard the rest. Counted and
                # logged with its traceback instead, and the row keeps whatever
                # it honestly knew before.
                logger.exception("bug while reading %s; recording nothing", url)
                result.internal_errors += 1
                result.internal_error_urls.append(url)
                continue
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
            # Before asking whether a 404 means absence, ask whether we even
            # sent the address that was cited. A row whose brackets do not
            # balance is probably a prefix of it, so nothing came back about the
            # source at all -- and no request is spent probing its container,
            # because that answer could not mean anything either.
            if outcome == GONE and (unbalanced_brackets(url) or is_vcs_requirement(url)):
                outcome = MALFORMED
            elif outcome == GONE and not absence_is_corroborated(
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
            # The redirect is named only when there WAS one. Printing
            # "(via itself)" on every ordinary dead link would bury the handful
            # of lines where the distinction is the whole point.
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
        set_content_hash(
            db_path,
            url,
            content_hash=digest,
            hashed_at=read_at,
            covers_bytes=read.covers_bytes,
            complete=read.complete,
        )
        result.hashed += 1
        result.hashed_urls.append(url)
        if not read.complete:
            result.partial += 1
            result.partial_urls.append(url)
            logger.info(
                "%s is larger than the cap; hashed its first %d bytes",
                url,
                read.covers_bytes,
            )
    return result


def verify(
    db_path,
    *,
    hasher: Callable[[str, int], PageRead],
    clock: Callable[[], str],
    limit: int | None = None,
    sleep_s: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_bytes: int = HASH_MAX_BYTES,
    only_outcome: str | None = None,
    only_host: str | None = None,
) -> VerifyResult:
    """Ask whether each hashed page still says what it said.

    Read-only against the LIBRARY, and against every column that holds evidence.
    A page that has changed is NOT re-hashed: the stored hash is the evidence of
    what was consulted, and quietly replacing it with what the page says today
    would destroy the finding at the moment it was made.

    It does now write two columns of its own, `verified_at` and
    `verify_outcome`, and the distinction is the whole design. Recording that we
    LOOKED is not recording what we FOUND. It is also the cursor: without it
    `rows_with_hash` could only paginate by position, so `--limit N` returned
    the same N rows on every run and a 3,813-row sweep was one all-or-nothing
    2.7-hour job or a permanently head-biased sample. `last_outcome` fixed
    exactly this defect in the other fetching pass; there was simply nothing
    here to inherit it from -- the same way `_HostPacer` was missing.

    `clock` is required and read once PER ROW, not per run, for the reason
    `hashed_at` was wrong for 1,348 rows: a timestamp threaded through a loop
    records the batch, not the event, and a chunked sweep makes that worse
    because the batch is now arbitrary. A parameter that CAN be omitted is the
    other failure this repo has shipped (`--sleep`, parsed for a whole release
    and passed to nothing), so it cannot be.

    `sleep_s` is not optional politeness. This pass re-reads EVERY hashed page in
    the corpus -- thousands of requests, many to hosts already rate-limiting us
    -- and it had no pacing at all while the CLI advertised `--sleep`. It shares
    `_HostPacer` with `snapshot` rather than keeping a second copy, because a
    second copy is how the first one came to be missing here.

    `only_host` and `only_outcome` narrow which rows are read, exactly as they
    do for `snapshot`. They existed on the CLI and reached only the other
    engine, so `--verify --only-host github.com` parsed cleanly, ignored the
    filter and swept all 3,813 rows -- the `--sleep` defect wearing a different
    hat, and invisible to the guard that derives obligations from argparse
    because `args.only_host` IS read, just on the path that does not run.

    Each row is re-read over THE SAME SPAN its stored digest covers. Comparing a
    5 MiB prefix against a 32 MiB one would report a change for every truncated
    row the moment the cap moved -- a finding about our own configuration wearing
    the costume of a finding about the source.
    """
    result = VerifyResult()
    pacer = _HostPacer(sleep_s, sleeper=sleeper, monotonic=monotonic)

    def conclude(url: str, outcome: str) -> None:
        """Stamp what this look concluded. Never called for one of OUR faults."""
        set_verify_outcome(db_path, url, outcome=outcome, at=clock())

    for row in rows_with_hash(
        db_path, limit=limit, only_outcome=only_outcome, only_host=only_host
    ):
        url = row["url_canonical"]
        result.examined += 1
        # A truncated row is re-read over exactly the span it recorded. A row
        # hashed whole is re-read under the current cap, and -1 means a legacy
        # row whose length was never recorded -- known complete, unknown size.
        stored_covers = row["hash_bytes"]
        was_truncated = bool(row["hash_truncated"])
        span = stored_covers if was_truncated and stored_covers >= 0 else max_bytes
        try:
            read = _paced_fetch(pacer, hasher, url, span)
            digest = read.digest
        except Exception as e:
            if not isinstance(e, FETCH_FAULTS):
                logger.exception("bug while re-reading %s; concluding nothing", url)
                result.internal_errors += 1
                continue
            # Split, not collapsed. A paywall refusing us and a citation that
            # died say different things, and reporting both as "unreachable"
            # is the conflation 0.36.0 removed from the other fetching pass.
            outcome = classify_failure(e)
            result.by_outcome[outcome] = result.by_outcome.get(outcome, 0) + 1
            logger.info("could not re-read %s (%s): %s", url, outcome, e)
            result.unreachable += 1
            result.unreachable_urls.append(url)
            # A closed door is a real observation, so it advances the cursor.
            # Not marking it is precisely what made 1,285 unreadable rows
            # re-fetch on every pass and block the head under `--limit`.
            conclude(url, outcome)
            continue
        if digest == row["content_hash"]:
            if was_truncated or not read.complete:
                # Agreement over a prefix. One-directional evidence: it could
                # have proved a change and it did not, which is not the same as
                # proving there was none.
                #
                # `not read.complete` catches the other direction too -- a row
                # stored whole that no longer fits. That means the document grew
                # OR our cap shrank, and for a legacy row (length -1) there is
                # nothing on hand to tell which. Guessing "changed" would invent
                # drift we never observed.
                result.partial_match += 1
                result.partial_match_urls.append(url)
                conclude(url, PREFIX_AGREED)
                continue
            result.unchanged += 1
            conclude(url, UNCHANGED)
            continue

        # A difference is a CANDIDATE, not a finding. Measured on this corpus:
        # 5 of 8 sampled pages produced a different digest when read twice three
        # seconds apart, because a whole-document hash of live HTML also covers
        # nonces, ad tokens, build ids and timestamps. Reporting the first
        # mismatch as "changed" is an affirmative claim about the SOURCE drawn
        # from one observation that cannot support it -- the same error as
        # reading a 404 as absence, which `absence_is_corroborated` exists to
        # prevent one layer along.
        #
        # So the claim is corroborated by reading again. Spent only on
        # candidates: an unchanged page never costs a second request.
        try:
            corroboration = _paced_fetch(pacer, hasher, url, span)
            second = corroboration.digest
        except Exception as e:
            if not isinstance(e, FETCH_FAULTS):
                logger.exception("bug while re-reading %s; concluding nothing", url)
                result.internal_errors += 1
                continue
            outcome = classify_failure(e)
            result.by_outcome[outcome] = result.by_outcome.get(outcome, 0) + 1
            logger.info("could not corroborate %s (%s): %s", url, outcome, e)
            result.unreachable += 1
            result.unreachable_urls.append(url)
            conclude(url, outcome)
            continue
        if second != digest:
            # It does not agree with ITSELF, so the WHOLE-RESPONSE digest says
            # nothing about whether the source moved.
            #
            # But the disagreement is itself evidence. Whatever two reads
            # seconds apart differ on is per-request by construction, so the
            # pair names the volatile bytes without anyone having to list them,
            # and what is left is the document. Measured 2026-09-02: on every
            # page sampled that residue was the entire page bar a nonce.
            stable = stable_digest(read, corroboration)
            if stable is None:
                # No usable alignment -- a page that genuinely rebuilds itself
                # between two reads. Declining is the same refusal
                # `prefix_agreed` makes: a digest over a fragment would be
                # worse than the silence it replaced.
                result.unstable += 1
                result.unstable_urls.append(url)
                conclude(url, UNSTABLE)
                continue
            if not row["stable_digest"]:
                set_stable_digest(
                    db_path, url, digest=stable.digest,
                    covers_bytes=stable.covers_bytes, algo=STABLE_ALGO,
                )
                result.stable_baseline += 1
                conclude(url, STABLE_BASELINE)
            elif row["stable_algo"] != STABLE_ALGO:
                # The stored digest was cut by a different method, so it is not
                # comparable with this one and a mismatch would say nothing
                # about the source. Re-baseline and SAY SO -- the one thing
                # this must never do is report the difference as drift, which
                # is the tool inventing the finding it exists to report.
                #
                # Any tag that is not this one, INCLUDING the empty string.
                # 0.53.0 shipped this as `row["stable_algo"] and ... != ...`,
                # and '' is the tag every pre-0.53.0 row carries -- so the guard
                # was unreachable for the 1,217 rows it existed for and five of
                # them were reported as DOCUMENT CHANGED on the first live run.
                # "Has a tag" and "has a DIFFERENT tag" are not the same
                # question; only the second one is about comparability.
                #
                # The order matters and is asserted: a row with no digest at all
                # also has no tag, and that is a first baseline, not a re-cut.
                set_stable_digest(
                    db_path, url, digest=stable.digest,
                    covers_bytes=stable.covers_bytes, algo=STABLE_ALGO,
                )
                result.stable_rebaselined += 1
                conclude(url, STABLE_REBASELINED)
            elif row["stable_digest"] == stable.digest:
                result.stable_unchanged += 1
                conclude(url, STABLE_UNCHANGED)
            else:
                result.stable_changed += 1
                result.stable_changed_urls.append(url)
                conclude(url, STABLE_CHANGED)
            continue
        if not was_truncated and not read.complete:
            # Stored whole, read short. The digests differ, but they cover
            # different spans, so the difference is not evidence about the
            # source -- see the note above.
            result.partial_match += 1
            result.partial_match_urls.append(url)
            conclude(url, PREFIX_AGREED)
            continue
        result.changed += 1
        result.changed_urls.append(url)
        conclude(url, CHANGED)
    result.unverified_remaining = unverified_count(db_path)
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
        f"  partial: {url}  (hashed to the cap; the tail is not covered)"
        for url in result.partial_urls
    ]
    lines += [
        f"  stamp refused: {url}  (item moved)" for url in result.stamp_refused_urls
    ]
    lines.append(f"examined      : {result.examined}")
    lines.append(f"hashed        : {result.hashed}")
    lines.append(f"unreachable   : {result.unreachable}")
    lines.append(f"  of those partial: {result.partial}")
    if result.internal_errors:
        lines.append(f"OUR BUGS      : {result.internal_errors}  (nothing recorded)")
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
    lines += [
        f"  prefix agreed: {url}  (only the first bytes are covered; "
        f"this is not 'unchanged')"
        for url in result.partial_match_urls
    ]
    lines += [
        f"  DOCUMENT CHANGED: {url}  (its per-request bytes aside)"
        for url in result.stable_changed_urls
    ]
    lines += [
        f"  unstable: {url}  (differs from itself; says nothing about drift)"
        for url in result.unstable_urls
    ]
    lines += [f"  unreachable: {url}" for url in result.unreachable_urls]
    lines.append(f"examined      : {result.examined}")
    lines.append(f"unchanged     : {result.unchanged}")
    lines.append(f"prefix agreed : {result.partial_match}  (tail never compared)")
    lines.append(f"CHANGED       : {result.changed}")
    lines.append(f"unreachable   : {result.unreachable}")
    lines.append(
        f"unstable      : {result.unstable}  (no usable alignment; nothing concluded)"
    )
    lines.append(f"stable baseline  : {result.stable_baseline}  (first look; recorded)")
    if result.stable_rebaselined:
        lines.append(
            f"re-baselined     : {result.stable_rebaselined}"
            "  (stored digest predates the current method; NOT a change in the source)"
        )
    lines.append(f"stable unchanged : {result.stable_unchanged}  (document agreed)")
    lines.append(f"DOCUMENT CHANGED : {result.stable_changed}")
    lines.append(f"never verified: {result.unverified_remaining}  (rows remaining)")
    if result.internal_errors:
        lines.append(f"OUR BUGS      : {result.internal_errors}  (nothing concluded)")
    if result.by_outcome:
        lines.append("why they could not be re-read:")
        for outcome, n in sorted(result.by_outcome.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {outcome:<14}: {n}")
    if result.changed:
        lines.append("")
        lines.append(
            "A changed page still carries its ORIGINAL hash: that is the record "
            "of what was consulted, and it is not overwritten with what the page "
            "says today."
        )
    return lines
