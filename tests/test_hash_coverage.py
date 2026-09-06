"""A hash that cannot say what it covers can only be all-or-nothing.

71 rows in the live index were fetched SUCCESSFULLY and hold no evidence at all:
no hash, no size, nothing any future pass could compare. They are the corpus's
biggest sources -- 39 arXiv PDFs, a Nature paper, an SEC filing, genome
assemblies -- and for exactly those the library can say nothing about what was
consulted, which is the one job it has.

The cause is not the cap and not memory. `hash_page` already streams. It is that
`content_hash` is a single column with no room to state its scope, so `''` means
BOTH "never read" and "read fine, refused to record" -- the same one-word-for-
two-findings defect that `unreachable` (gone vs blocked) and `gone` (absent vs
not-visible) already cost this project a release each. Given that column, the
old rule was not a choice: a prefix hash stored where a whole-document hash is
expected WOULD compare equal for two documents differing after the cap, and the
docstring defending that was right about everything except the schema.

Measured before any of this was written, because the shape of the number decides
the fix: median 11.8 MiB, and the largest is a 207 GiB zip. No cap that reaches
the tail is defensible, so "raise it" cannot be the answer -- it moves the
tripwire and leaves the same silence for the datasets.

HTTP (`Content-Range`, `Repr-Digest`), git blobs and BitTorrent pieces all answer
this the same way: a digest is always scoped and the scope travels WITH it. None
of them has a whole-thing-or-nothing mode. That is the same shape this module
already adopted for `PageRead(digest, final_url)`, where returning a bare digest
was the bug because the address it belonged to died at the boundary.

So a capped read stops being an exception and becomes a read with stated
coverage. The consequence that carries the whole feature: a prefix hash is
ONE-DIRECTIONAL. If it changes, the document changed. If it does not, we have
learned nothing about the tail -- and `verify` must be unable to call that
"unchanged", for precisely the reason a 404 must be unable to become "gone".
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import inspect
from pathlib import Path

import httpx
import pytest

from zotero_capture.snapshot import (
    HASH_MAX_BYTES,
    PageRead,
    hash_page,
    snapshot,
    verify,
)
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    row_for_url,
    set_content_hash,
)

URL = "https://fixturehost.org/big"

# The boundary is tested at a SMALL explicit cap rather than at the real one.
# The property is "the cut lands at max_bytes", which does not depend on the
# value -- and building a 33 MiB body for each of these added minutes to the
# suite the moment the default cap was raised. A slow suite is a suite that gets
# run less, which is its own defect. `test_the_default_cap_is_the_one_that_ships`
# keeps the constant itself pinned.
CAP = 8192
BIG = b"y" * (CAP + 4096)


def _client(body: bytes, status: int = 200) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


class _Stamper:
    def __init__(self) -> None:
        self.stamped: list[tuple[str, str]] = []

    def record_content_hash(self, item_key, digest, *, expect_url=None):
        self.stamped.append((item_key, digest))
        return True


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


# --- a read is always scoped -------------------------------------------------


def test_an_oversized_document_is_hashed_to_a_stated_prefix() -> None:
    """The defect, directly. This used to raise and record nothing at all."""
    with _client(BIG) as http:
        read = hash_page(URL, client=http, max_bytes=CAP)
    assert read.digest, "an oversized document produced no evidence whatsoever"
    assert read.complete is False
    assert read.covers_bytes == CAP


def test_the_prefix_digest_is_of_EXACTLY_the_first_n_bytes() -> None:
    """A partial hash is only worth something if it is a defined value rather
    than whatever happened to be buffered when we stopped. Two runs against the
    same document must agree, or every re-read reports a spurious change."""
    with _client(BIG) as http:
        read = hash_page(URL, client=http, max_bytes=CAP)
    assert read.digest == hashlib.sha256(BIG[:CAP]).hexdigest()


def test_a_chunk_boundary_does_not_move_the_cut() -> None:
    """The cut must land at the cap, not at the end of whichever chunk crossed
    it. Chunk sizes are the transport's business and vary between runs, so a
    digest that depended on them would differ for a document that had not."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BIG)

    digests = set()
    for _ in range(2):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            digests.add(hash_page(URL, client=http, max_bytes=CAP).digest)
    assert len(digests) == 1
    assert digests.pop() == hashlib.sha256(BIG[:CAP]).hexdigest()


def test_a_document_within_the_cap_is_complete_and_says_so() -> None:
    """Positive control. A fix that marked everything partial would satisfy every
    assertion above while destroying the only strong claim this tool makes."""
    body = b"small"
    with _client(body) as http:
        read = hash_page(URL, client=http)
    assert read.complete is True
    assert read.covers_bytes == len(body)
    assert read.digest == hashlib.sha256(body).hexdigest()


def test_the_cap_is_a_parameter_not_a_constant() -> None:
    """Because it is now a cost bound rather than an honesty bound. While a
    partial read was inexpressible the cap decided whether evidence existed at
    all; now it only decides how much we pay for it."""
    with _client(BIG) as http:
        read = hash_page(URL, client=http, max_bytes=1024)
    assert read.covers_bytes == 1024
    assert read.complete is False
    assert read.digest == hashlib.sha256(BIG[:1024]).hexdigest()


def test_a_page_read_cannot_be_built_without_its_coverage() -> None:
    """The mechanism, not a convention. `final_url` was made required for the
    same reason: a parameter that CAN be omitted eventually is, invisibly from
    both ends -- this repository shipped `--sleep` parsed-and-dropped for a whole
    release. A digest with a defaulted scope is a digest that lies by omission.
    """
    with pytest.raises(TypeError):
        PageRead(digest="d", final_url=URL)  # type: ignore[call-arg]


# --- the index records the scope --------------------------------------------


def test_the_stored_hash_carries_its_coverage(db: Path) -> None:
    insert_url(db, URL, "K1", datetime.date(2026, 9, 1))
    set_content_hash(
        db, URL, content_hash="abc", hashed_at="T", covers_bytes=1024, complete=False,
        sketch="", sketch_algo="",
    )
    row = row_for_url(db, URL)
    assert row["hash_bytes"] == 1024
    assert row["hash_truncated"] == 1


def test_a_hash_cannot_be_stored_without_saying_what_it_covers(db: Path) -> None:
    """Required, not defaulted. A default would be "complete", so a caller that
    simply forgot would upgrade a prefix into a whole-document claim -- which is
    the exact false negative the old all-or-nothing rule existed to prevent."""
    insert_url(db, URL, "K1", datetime.date(2026, 9, 1))
    with pytest.raises(TypeError):
        set_content_hash(db, URL, content_hash="abc", hashed_at="T", sketch="", sketch_algo="")  # type: ignore[call-arg]


def test_rows_hashed_before_this_existed_read_as_COMPLETE(db: Path) -> None:
    """~3,744 live rows were hashed under the all-or-nothing rule, so every one
    of them IS complete. A migration defaulting them to "partial" would silently
    demote the entire corpus to inconclusive; defaulting the LENGTH to a number
    would invent one. Unknown length, known completeness -- that is what actually
    happened."""
    insert_url(db, URL, "K1", datetime.date(2026, 9, 1))
    with __import__("sqlite3").connect(db) as conn:
        conn.execute(
            "UPDATE url_index SET content_hash = 'legacy', hashed_at = 'T'"
            " WHERE url_canonical = ?",
            (URL,),
        )
    row = row_for_url(db, URL)
    assert row["hash_truncated"] == 0, "a legacy whole-document hash read as partial"
    assert row["hash_bytes"] == -1, "a length was invented for a row that has none"


def test_an_oversized_page_is_now_recorded_by_the_snapshot_pass(db: Path) -> None:
    """End to end: the 71 rows this release exists for. `too_large` was an
    outcome meaning "we refused"; a read that produced a scoped digest is not a
    refusal, it is a read, so the outcome is `ok` and the partialness lives in
    the coverage columns rather than in a second vocabulary that can drift from
    them. This project has now paid four times for one rule kept in two places.
    """
    insert_url(db, URL, "K1", datetime.date(2026, 9, 1))

    def hasher(url: str, max_bytes: int) -> PageRead:
        return PageRead(
            digest="partial-digest",
            final_url=url,
            covers_bytes=max_bytes,
            complete=False,
        )

    result = snapshot(db, zotero=_Stamper(), hasher=hasher, clock=lambda: "T")

    assert result.hashed == 1
    assert result.partial == 1
    row = row_for_url(db, URL)
    assert row["content_hash"] == "partial-digest"
    assert row["last_outcome"] == "ok"
    assert row["hash_truncated"] == 1


# --- a prefix proves change, never its absence -------------------------------


def _seed(db, url, *, digest, covers, complete):
    insert_url(db, url, "K1", datetime.date(2026, 9, 1))
    set_content_hash(
        db, url, content_hash=digest, hashed_at="THEN",
        covers_bytes=covers, complete=complete,
        sketch="", sketch_algo="",
    )
    return db


def test_a_matching_PREFIX_is_not_reported_as_unchanged(db: Path) -> None:
    """The property this whole release turns on, and the one a careless fix
    loses. "The first 5 MiB of a 39 MiB PDF are unchanged" is not "the PDF is
    unchanged" -- the tail was never compared and a document edited near its end
    is the exact case the hash exists to catch. Calling it unchanged would
    manufacture a reassurance, which is worse than the silence it replaced."""
    _seed(db, URL, digest="P", covers=1024, complete=False)

    result = verify(
        db, clock=lambda: "NOW", hasher=lambda u, n: PageRead(
            digest="P", final_url=u, covers_bytes=n, complete=False
        ),
    )

    assert result.unchanged == 0, "a prefix match was reported as the whole document"
    assert result.partial_match == 1


def test_a_matching_COMPLETE_hash_is_still_reported_unchanged(db: Path) -> None:
    """Positive control. A fix that simply stopped ever saying "unchanged" would
    pass the test above and destroy the strongest finding this pass produces."""
    _seed(db, URL, digest="C", covers=500, complete=True)

    result = verify(
        db, clock=lambda: "NOW", hasher=lambda u, n: PageRead(
            digest="C", final_url=u, covers_bytes=500, complete=True
        ),
    )

    assert result.unchanged == 1
    assert result.partial_match == 0


def test_a_DIFFERING_prefix_is_still_a_change(db: Path) -> None:
    """The other direction, and why a partial hash is worth storing at all. It
    is one-directional: it cannot prove sameness, but a difference inside the
    covered range proves the document moved."""
    _seed(db, URL, digest="P", covers=1024, complete=False)
    reads = iter(["Q", "Q"])  # agrees with itself, so the change is corroborated

    result = verify(
        db, clock=lambda: "NOW", hasher=lambda u, n: PageRead(
            digest=next(reads), final_url=u, covers_bytes=n, complete=False
        ),
    )

    assert result.changed == 1
    assert result.partial_match == 0


def test_verify_re_reads_THE_SAME_number_of_bytes_it_stored(db: Path) -> None:
    """Otherwise every truncated row reports a change the moment the cap moves,
    which is a finding about our configuration wearing the costume of a finding
    about the source."""
    _seed(db, URL, digest="P", covers=777, complete=False)
    asked: list[int] = []

    def hasher(url: str, max_bytes: int) -> PageRead:
        asked.append(max_bytes)
        return PageRead(digest="P", final_url=url, covers_bytes=max_bytes, complete=False)

    verify(db, clock=lambda: "NOW", hasher=hasher)
    assert asked == [777], f"re-read a different span than it stored: {asked}"


def test_a_complete_row_that_no_longer_fits_is_NOT_called_changed(db: Path) -> None:
    """A row hashed complete that now reads truncated could mean the document
    grew -- or that OUR cap shrank. For a legacy row (length -1) we cannot tell
    which, and guessing "changed" would invent link-rot's cousin: drift we never
    observed. Inconclusive is the honest bucket."""
    insert_url(db, URL, "K1", datetime.date(2026, 9, 1))
    with __import__("sqlite3").connect(db) as conn:
        conn.execute(
            "UPDATE url_index SET content_hash = 'legacy', hashed_at = 'T'"
            " WHERE url_canonical = ?",
            (URL,),
        )

    result = verify(
        db, clock=lambda: "NOW", hasher=lambda u, n: PageRead(
            digest="other", final_url=u, covers_bytes=n, complete=False
        ),
    )

    assert result.changed == 0, "a cap change was reported as the source changing"
    # 0.61.0: not "prefix agreed" either -- the digests differ and the spans
    # do not correspond, so nothing was compared. Incomparable is the word.
    assert result.partial_match == 0
    assert result.incomparable == 1


# --- the flag reaches the thing that fetches ---------------------------------


CLI = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_pages.py"


def test_the_cap_flag_is_parsed_and_actually_delivered() -> None:
    """The dead-flag class, guarded at birth this time. `--sleep` was accepted,
    documented as politeness, and dropped for a whole release; `--max-bytes` has
    the identical shape, so it gets the identical check before it ships rather
    than after a run goes out under a promise it did not keep."""
    tree = ast.parse(CLI.read_text())
    flags = {
        str(a.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_argument"
        for a in n.args
        if isinstance(a, ast.Constant)
    }
    assert "--max-bytes" in flags

    used = {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "args"
    }
    assert "max_bytes" in used, "--max-bytes is parsed and then never read"


def test_hash_page_still_accepts_the_cap_by_keyword() -> None:
    """So the CLI cannot pass it positionally into the wrong slot."""
    params = inspect.signature(hash_page).parameters
    assert params["max_bytes"].kind is inspect.Parameter.KEYWORD_ONLY


# --- a bug of ours is not a finding about a citation -------------------------


def test_a_bug_in_the_hasher_is_not_recorded_as_link_rot(db: Path) -> None:
    """Found by this release, not looked for. Changing the hasher's signature
    made the old stubs raise TypeError; `except Exception` caught it and stamped
    20 rows `unreachable` -- our own bug written into the library as a fact about
    somebody's citation, which is the single worst thing this tool can do.

    `classify_failure` was right to send anything unrecognised to UNREACHABLE
    rather than GONE, but that was a rule for unrecognised NETWORK errors. A
    TypeError is not a fact about the source at any confidence.
    """
    insert_url(db, URL, "K1", datetime.date(2026, 9, 1))

    def broken(url: str, max_bytes: int) -> PageRead:
        raise TypeError("hasher() takes 1 positional argument but 2 were given")

    result = snapshot(db, zotero=_Stamper(), hasher=broken, clock=lambda: "T")

    assert result.internal_errors == 1
    assert result.unreachable == 0, "our bug was counted as an unreadable source"
    assert row_for_url(db, URL)["last_outcome"] == "", (
        "a fact about the source was written from an error that was about us"
    )


def test_a_real_http_failure_is_still_classified(db: Path) -> None:
    """The positive control, and the one that matters. A fix that simply routed
    every exception to "our bug" would pass the test above while silently
    deleting the entire outcome vocabulary -- 750 paywalls and 42 dead links
    would all become internal errors and the report would go blank."""
    insert_url(db, URL, "K1", datetime.date(2026, 9, 1))

    def refused(url: str, max_bytes: int) -> PageRead:
        raise httpx.HTTPStatusError(
            "403",
            request=httpx.Request("GET", url),
            response=httpx.Response(403, request=httpx.Request("GET", url)),
        )

    result = snapshot(db, zotero=_Stamper(), hasher=refused, clock=lambda: "T")

    assert result.internal_errors == 0
    assert result.unreachable == 1
    assert row_for_url(db, URL)["last_outcome"] == "blocked"


def test_verify_concludes_nothing_from_a_bug_of_ours(db: Path) -> None:
    """The same rule on the other fetching pass, because one rule kept in two
    places is how this repository has lost four releases."""
    _seed(db, URL, digest="C", covers=500, complete=True)

    def broken(url: str, max_bytes: int) -> PageRead:
        raise AttributeError("no attribute 'digest'")

    result = verify(db, clock=lambda: "NOW", hasher=broken)

    assert result.internal_errors == 1
    assert (result.changed, result.unchanged, result.unreachable) == (0, 0, 0)


def test_the_default_cap_is_the_one_that_ships() -> None:
    """The tests above run at a small explicit cap, so the shipped constant needs
    its own pin -- otherwise the default could drift to any value and every
    boundary test would still be green."""
    assert HASH_MAX_BYTES == 32 * 1024 * 1024


def test_an_unusable_stored_address_is_malformed_not_a_bug() -> None:
    """`httpx.InvalidURL` is the one fetch failure that is NOT an
    `httpx.HTTPError`, checked against the live hierarchy rather than recalled.
    That matters twice over: it must not be swept into "our bug" (it is a real
    finding about the row), and it must never have been `unreachable` (which
    blamed a host we never managed to ask).

    It says the string we STORED is not a usable address -- which is what
    MALFORMED already means here, and the reason that outcome exists.
    """
    from zotero_capture.snapshot import MALFORMED, classify_failure

    assert classify_failure(httpx.InvalidURL("no host")) == MALFORMED


def test_every_httpx_error_is_deliberately_on_one_side_or_the_other() -> None:
    """Derived from httpx itself, not from the errors this corpus happened to
    produce. A guard that named the exceptions seen so far cannot fail on the one
    it has not met -- the defect that made both the User-Agent guard and the
    `--sleep` guard pass on broken code.

    It found one on its first run: `CookieConflict`, which is API misuse and so
    belongs with our bugs, not with a citation's failures. The partition is
    written out because that is the decision; if httpx adds an exception, this
    goes red and somebody has to say which side it is on, rather than it
    defaulting quietly into "the source is unreachable".
    """
    import inspect

    # Raised when WE drive the client wrongly. Nothing about a source.
    OURS = (httpx.StreamError, httpx.CookieConflict)
    # Raised by a fetch, or by the address we stored. A finding about the row.
    THEIRS = (httpx.HTTPError, httpx.InvalidURL)

    unclassified = [
        obj.__name__
        for _, obj in inspect.getmembers(httpx, inspect.isclass)
        if issubclass(obj, Exception) and not issubclass(obj, OURS + THEIRS)
    ]
    assert unclassified == [], (
        f"httpx errors nobody has decided about: {unclassified}. Each is either "
        f"a fetch failure (record an outcome) or our own misuse (record nothing)."
    )


# --- 0.61.0: "prefix agreed" was stamped when nothing agreed -------------------


def test_a_short_re_read_that_DIFFERS_is_incomparable_not_agreed(db: Path) -> None:
    """Stored whole, re-read short, digests DIFFERENT. The spans do not
    overlap in any comparable way, so nothing was concluded -- and the row
    was stamped `prefix_agreed`, whose report line says "only the first
    bytes are covered". Nothing agreed. `unreachable` covering gone AND
    blocked was this same collapse; the word is the defect."""
    import sqlite3

    from zotero_capture.snapshot import INCOMPARABLE

    _seed(db, URL, digest="OLDWHOLE", covers=64, complete=True)
    result = verify(
        db, clock=lambda: "NOW",
        hasher=lambda u, n: PageRead(digest="NEWPREFIX", final_url=u, covers_bytes=n, complete=False),
    )
    assert result.incomparable == 1 and result.incomparable_urls == [URL]
    assert result.partial_match == 0
    assert result.changed == 0
    stored = sqlite3.connect(db).execute(
        "SELECT verify_outcome FROM url_index WHERE url_canonical = ?", (URL,)
    ).fetchone()[0]
    assert stored == INCOMPARABLE


def test_a_short_re_read_that_MATCHES_is_still_prefix_agreed(db: Path) -> None:
    """Positive control: the word survives where it is true."""
    _seed(db, URL, digest="P", covers=777, complete=False)
    result = verify(
        db, clock=lambda: "NOW",
        hasher=lambda u, n: PageRead(digest="P", final_url=u, covers_bytes=n, complete=False),
    )
    assert result.partial_match == 1
    assert result.incomparable == 0
