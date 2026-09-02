"""Hash what two reads agree on, so a nonce stops reading as provenance drift.

`unstable` meant "the page did not read the same way twice", and 1,807 of 3,813
rows -- 47% of the corpus -- ended there. Measured on 2026-09-02 by fetching
pages twice seconds apart and diffing them, that verdict is almost never about
the source:

    github.com/snap-stanford/Biomni   identical byte length, ONE differing
                                      line of 1,365: <meta name="request-id">

A GitHub pull request differed in 100 lines of 2,354, and every one was a
session token, a CSRF field, a per-render UUID wiring a button to its tooltip,
or an A/B bucket. Not a GitHub property either: doi.org, nature.com, ncbi,
huggingface and springer all differ by under 1.5% of lines, all telemetry.

So the digest was covering the transport envelope rather than the document, and
`unstable` was a finding about our METHOD reported as a finding about theirs.

The fix is not a list of volatile field names. A hand-maintained list is the
class this repository has already been burned by twice -- `URL_RE`'s character
blacklist, and a User-Agent guard that named its call sites and so could not see
a fourth copy appear. Instead the volatile bytes are DERIVED from the two reads
`verify` already performs: whatever the pair disagrees on is per-request by
definition, and the digest covers the rest. No host knowledge, and it works on
hosts that have not been written yet.

Measured before it was built (three reads, does the digest reproduce?):

    stable(A,B) == stable(B,C)      5 of 6 sampled pages
    anthropic.com/research/...      NO -- kept 0 of 1 lines

That failure is why `MIN_STABLE_COVERAGE` exists. A single-line minified
document has no line structure to align, so the honest answer is to decline and
leave the row `unstable` -- a bogus digest over a fragment would be worse than
the silence it replaced, which is the mistake `prefix_agreed` exists to prevent
one layer along.
"""

from __future__ import annotations

import ast
import datetime
import hashlib
from pathlib import Path

import zotero_capture.snapshot as snap
from zotero_capture.snapshot import (
    STABLE_BASELINE,
    STABLE_CHANGED,
    STABLE_UNCHANGED,
    UNSTABLE,
    PageRead,
    stable_digest,
    verify,
)
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    row_for_url,
    set_content_hash,
    set_stable_digest,
)

NONCE = "<meta name=session content={}>"
# Deliberately a realistic ratio. A three-line fixture makes one token a third
# of the document, which no real page looks like: the GitHub page this was
# measured on is 399 KB with ONE volatile line. A fixture with the wrong shape
# would have driven the coverage floor to a number the corpus never justified.
BODY = (
    ["<html>"]
    + [f"<p>paragraph {i} of the cited work</p>" for i in range(20)]
    + ["</html>"]
)
EDITED = (
    ["<html>"]
    + [f"<p>paragraph {i} of the cited work</p>" for i in range(19)]
    + ["<p>this paragraph was rewritten after we cited it</p>", "</html>"]
)


def _lines(text_lines):
    return tuple(
        snap.LineRead(hashlib.sha256(x.encode()).hexdigest(), len(x))
        for x in text_lines
    )


def _read(text_lines, *, digest="D", complete=True) -> PageRead:
    total = sum(len(x) for x in text_lines) if text_lines is not None else 0
    return PageRead(
        digest=digest,
        final_url="https://example.org/p",
        covers_bytes=total,
        complete=complete,
        lines=None if text_lines is None else _lines(text_lines),
    )


def _page(n: int):
    """The same document, read again, with only the per-request line moved."""
    return BODY[:1] + [NONCE.format(n)] + BODY[1:]


def _edited(n: int):
    """The same document after a real edit, still carrying a moving token."""
    return EDITED[:1] + [NONCE.format(n)] + EDITED[1:]


# --- the digest itself --------------------------------------------------------


def test_a_page_differing_only_by_a_nonce_still_yields_a_digest() -> None:
    s = stable_digest(_read(_page(1)), _read(_page(2)))
    assert s is not None
    # It covers the document and not the token: every BODY byte, no NONCE byte.
    assert s.covers_bytes == sum(len(x) for x in BODY)


def test_the_digest_reproduces_across_a_third_read() -> None:
    """The property that makes this a digest rather than a coincidence.

    Measured live on six pages before this was written; five reproduced. If the
    volatile set shifted between reads the digest would not be comparable across
    passes, and the whole idea would be worthless.
    """
    ab = stable_digest(_read(_page(1)), _read(_page(2)))
    bc = stable_digest(_read(_page(2)), _read(_page(3)))
    assert ab is not None and bc is not None
    assert ab.digest == bc.digest


def test_a_real_edit_moves_the_digest() -> None:
    """Positive control. Every assertion above narrows what counts as a change;
    a function that returned a constant would satisfy all of them."""
    before = stable_digest(_read(_page(1)), _read(_page(2)))
    after = stable_digest(_read(_edited(9)), _read(_edited(10)))
    assert before is not None and after is not None
    assert before.digest != after.digest


def test_a_single_line_minified_document_declines_rather_than_guessing() -> None:
    """The one page of six that did not reproduce, and why the floor exists.

    Both directions in one test: the SAME content, given line structure, does
    yield a digest. Without that control, "returns None" would also pass on a
    function that had simply stopped working.
    """
    minified_a = ["".join(_page(1))]
    minified_b = ["".join(_page(2))]
    assert stable_digest(_read(minified_a), _read(minified_b)) is None

    assert stable_digest(_read(_page(1)), _read(_page(2))) is not None


def test_a_page_that_mostly_disagrees_declines() -> None:
    a = ["stable"] + [f"volatile-{i}" for i in range(20)]
    b = ["stable"] + [f"volatile-{i}x" for i in range(20)]
    assert stable_digest(_read(a), _read(b)) is None


def test_unrecorded_lines_never_produce_a_digest() -> None:
    """`lines=None` means "we did not record them", not "there were none".

    One value meaning two things is the defect that cost this project a release
    each for `unreachable` (gone vs blocked) and `gone` (absent vs not-visible).
    """
    assert stable_digest(_read(None), _read(_page(1))) is None
    assert stable_digest(_read(_page(1)), _read(None)) is None
    assert stable_digest(_read(None), _read(None)) is None


# --- producing the line digests while streaming --------------------------------


def test_line_digests_do_not_depend_on_how_the_body_was_chunked() -> None:
    """Chunk sizes belong to the transport.

    `hash_page` already says so about the byte cap -- "a digest that depended on
    them would differ for a document that had not". The same trap is one layer
    down here: a chunk boundary landing mid-line must not invent two lines.
    """
    text = _page(1)
    body = b"\n".join(x.encode() for x in text)

    # The property, not a proxy. Comparing split-fed against whole-fed only
    # asserts they AGREE -- both dropping the unterminated last line satisfies
    # that and loses a line of the document. Mutation-tested: asserting only
    # agreement did not notice the trailing line being discarded.
    expected = tuple(
        snap.LineRead(hashlib.sha256(x.encode()).hexdigest(), len(x)) for x in text
    )
    whole = snap._LineDigester()
    whole.update(body)
    assert whole.finish() == expected

    for size in (1, 3, 7, 13, len(body) - 1):
        split = snap._LineDigester()
        for i in range(0, len(body), size):
            split.update(body[i : i + size])
        assert split.finish() == expected, f"chunk size {size}"


def test_a_recorded_baseline_is_never_overwritten(tmp_path) -> None:
    """The write-once rule, driven directly rather than through `verify`.

    Mutation-tested: dropping the guard from the UPDATE changed nothing that
    any test could see, because `verify` reaches it only on the first look. A
    guard the production caller cannot currently feed is a guard nothing holds
    -- this repository shipped two of those in one release and had to delete
    them. Driving it here is what turns it from decoration into a rule.
    """
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    assert set_stable_digest(
        db, "https://example.org/p", digest="FIRST", covers_bytes=10
    )
    assert not set_stable_digest(
        db, "https://example.org/p", digest="SECOND", covers_bytes=20
    )
    row = row_for_url(db, "https://example.org/p")
    assert row["stable_digest"] == "FIRST"
    assert row["stable_bytes"] == 10


def test_too_many_lines_records_nothing_rather_than_a_partial_list() -> None:
    d = snap._LineDigester(max_lines=3)
    d.update(b"a\nb\nc\nd\ne\n")
    assert d.finish() is None


# --- what verify does with it --------------------------------------------------


def _seed(db, url, *, stored_hash="OLD"):
    init_db(db)
    insert_url(db, url, "K1", datetime.date(2026, 5, 5))
    set_content_hash(
        db,
        url,
        content_hash=stored_hash,
        hashed_at="T1",
        covers_bytes=64,
        complete=True,
    )
    return db


def _volatile_hasher(pages):
    """Returns a different nonce every call, like a real page does."""
    seq = iter(pages)

    def hasher(url: str, max_bytes: int) -> PageRead:
        text = next(seq)
        return PageRead(
            digest=hashlib.sha256("".join(text).encode()).hexdigest(),
            final_url=url,
            covers_bytes=sum(len(x) for x in text),
            complete=True,
            lines=_lines(text),
        )

    return hasher


def _run(db, hasher):
    return verify(db, hasher=hasher, clock=lambda: "NOW")


def test_the_first_unstable_look_records_a_baseline(tmp_path) -> None:
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    result = _run(db, _volatile_hasher([_page(1), _page(2)]))
    row = row_for_url(db, "https://example.org/p")
    assert row["verify_outcome"] == STABLE_BASELINE
    assert row["stable_digest"] != ""
    assert result.stable_baseline == 1
    # The evidence of what was consulted is never rewritten.
    assert row["content_hash"] == "OLD"


def test_a_later_look_that_agrees_is_stable_unchanged(tmp_path) -> None:
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    _run(db, _volatile_hasher([_page(1), _page(2)]))
    result = _run(db, _volatile_hasher([_page(3), _page(4)]))
    row = row_for_url(db, "https://example.org/p")
    assert row["verify_outcome"] == STABLE_UNCHANGED
    assert result.stable_unchanged == 1


def test_a_later_look_that_disagrees_is_stable_changed(tmp_path) -> None:
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    _run(db, _volatile_hasher([_page(1), _page(2)]))
    before = row_for_url(db, "https://example.org/p")["stable_digest"]

    result = _run(db, _volatile_hasher([_edited(5), _edited(6)]))

    row = row_for_url(db, "https://example.org/p")
    assert row["verify_outcome"] == STABLE_CHANGED
    assert result.stable_changed == 1
    # A finding is not erased by the pass that made it.
    assert row["stable_digest"] == before


def test_a_minified_page_stays_unstable_and_stores_nothing(tmp_path) -> None:
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    result = _run(db, _volatile_hasher([["".join(_page(1))], ["".join(_page(2))]]))
    row = row_for_url(db, "https://example.org/p")
    assert row["verify_outcome"] == UNSTABLE
    assert row["stable_digest"] == ""
    assert result.unstable == 1


def test_a_page_that_agrees_with_itself_never_reaches_this_path(tmp_path) -> None:
    """Positive control for the whole feature: it must not touch stable pages.

    A page whose digest matches the stored one is `unchanged`, costs one
    request, and gets no stable digest -- there was no disagreement to derive
    one from.
    """
    db = _seed(tmp_path / "i.db", "https://example.org/p", stored_hash="MATCH")

    def hasher(url, max_bytes):
        return PageRead(
            digest="MATCH",
            final_url=url,
            covers_bytes=64,
            complete=True,
            lines=_lines(_page(1)),
        )

    result = _run(db, hasher)
    row = row_for_url(db, "https://example.org/p")
    assert result.unchanged == 1
    assert row["stable_digest"] == ""


# --- the design, locked in -----------------------------------------------------


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def test_the_fix_is_not_a_list_of_volatile_field_names() -> None:
    """The design decision, held by a test rather than by memory.

    Naming `request-id`, `csrf` and friends would work on today's sample and
    rot silently -- it cannot fail on a token that does not exist yet, which is
    the same defect in a guard that the guard exists to prevent in the code.
    """
    named = (
        "csrf",
        "request-id",
        "html-safe-nonce",
        "visitor-payload",
        "octolytics",
        "data-turbo",
        "ui-target",
        "fetch-nonce",
    )
    tree = ast.parse(Path(snap.__file__).read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    offenders = [
        f"{node.lineno}:{node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and any(n in node.value.lower() for n in named)
    ]
    assert offenders == [], (
        f"volatility is being named rather than derived: {offenders}"
    )
    # Non-vacuous: the thing that replaces the list has to exist.
    assert callable(stable_digest)
