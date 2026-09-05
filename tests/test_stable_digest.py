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


def _units(text_lines):
    return tuple(
        snap.UnitRead(hashlib.sha256(x.encode()).hexdigest(), len(x))
        for x in text_lines
    )


def _read(text_lines, *, digest="D", complete=True) -> PageRead:
    total = sum(len(x) for x in text_lines) if text_lines is not None else 0
    return PageRead(
        digest=digest,
        final_url="https://example.org/p",
        covers_bytes=total,
        complete=complete,
        units=None if text_lines is None else _units(text_lines),
    )


def _page(n: int):
    """The same document, read again, with only the per-request line moved."""
    return BODY[:1] + [NONCE.format(n)] + BODY[1:]


def _edited(n: int):
    """The same document after a real edit, still carrying a moving token."""
    return EDITED[:1] + [NONCE.format(n)] + EDITED[1:]


# EDITED moves ONE paragraph of 22. Under the equality test that was a
# detectable change, but so was a page that had not changed at all -- the
# comparison answered "different" for every pair of reads, which is how 175
# rows came to hold a `stable_changed` verdict that was really our own
# sampling. A test asserting "changed" against a method that says changed for
# everything cannot fail, so the two below were passing for the wrong reason.
#
# Similarity measures overlap, so a change has to clear the noise that moving
# domains produce. Measured live: unchanged pages sit at 0.958-1.000, two
# DIFFERENT articles at 0.239-0.595. This fixture is the second shape -- the
# document replaced rather than tweaked -- which is what the method now claims
# to detect. The tweak it can no longer see has its own test below, stating the
# cost instead of hiding it.
REPLACED = (
    ["<html>"]
    + [f"<p>an entirely different sentence {i}</p>" for i in range(20)]
    + ["</html>"]
)


def _replaced(n: int):
    """A different document at the same address, still carrying a moving token."""
    return REPLACED[:1] + [NONCE.format(n)] + REPLACED[1:]


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


def test_units_that_do_not_align_decline_rather_than_guessing() -> None:
    """The floor, driven directly: given units that disagree, decline.

    Until 0.53.0 this test was called "a single-line minified document declines"
    and it described production, because the unit WAS the line: a minified page
    is one line, so one token anywhere in it aligned to nothing. That is the
    defect 0.53.0 removed, and production no longer produces this shape from a
    minified page -- `test_content_defined_units.py` drives that end to end.
    What survives here is the property this function is responsible for on its
    own, whatever cut the units.

    Both directions in one test: the SAME content, given units that align, does
    yield a digest. Without that control, "returns None" would also pass on a
    function that had simply stopped working.
    """
    one_big_unit_a = ["".join(_page(1))]
    one_big_unit_b = ["".join(_page(2))]
    assert stable_digest(_read(one_big_unit_a), _read(one_big_unit_b)) is None

    assert stable_digest(_read(_page(1)), _read(_page(2))) is not None


def test_a_page_that_mostly_disagrees_declines() -> None:
    a = ["stable"] + [f"volatile-{i}" for i in range(20)]
    b = ["stable"] + [f"volatile-{i}x" for i in range(20)]
    assert stable_digest(_read(a), _read(b)) is None


def test_unrecorded_units_never_produce_a_digest() -> None:
    """`units=None` means "we did not record them", not "there were none".

    One value meaning two things is the defect that cost this project a release
    each for `unreachable` (gone vs blocked) and `gone` (absent vs not-visible).
    """
    assert stable_digest(_read(None), _read(_page(1))) is None
    assert stable_digest(_read(_page(1)), _read(None)) is None
    assert stable_digest(_read(None), _read(None)) is None


# --- producing the unit digests while streaming --------------------------------


def test_unit_digests_do_not_depend_on_how_the_body_was_chunked() -> None:
    """Transport chunk sizes belong to the transport.

    `hash_page` already says so about the byte cap -- "a digest that depended on
    them would differ for a document that had not". The same trap is one layer
    down here, and the rolling hash is what makes it a live risk: its state has
    to carry ACROSS an `iter_bytes` boundary, so a boundary landing mid-unit
    must not become a cut.

    The byte total is asserted as well as the agreement, and that is the half
    that carries the test. Comparing split-fed against whole-fed only says they
    AGREE -- both losing the unterminated tail satisfies that while dropping the
    end of the document. Mutation-tested on the line version this replaces:
    asserting only agreement did not notice a discarded trailing line.
    """
    body = b"\n".join(x.encode() for x in _page(1)) * 40

    whole = snap._ChunkDigester()
    whole.update(body)
    expected = whole.finish()
    assert expected is not None
    assert sum(u.nbytes for u in expected) == len(body)
    assert len(expected) > 1, "fixture too small to have any boundaries to get wrong"

    for size in (1, 3, 7, 13, 1024, len(body) - 1):
        split = snap._ChunkDigester()
        for i in range(0, len(body), size):
            split.update(body[i : i + size])
        assert split.finish() == expected, f"transport chunk size {size}"


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
        db, "https://example.org/p", digest="FIRST", covers_bytes=10,
        algo=snap.STABLE_ALGO,
    )
    assert not set_stable_digest(
        db, "https://example.org/p", digest="SECOND", covers_bytes=20,
        algo=snap.STABLE_ALGO,
    )
    row = row_for_url(db, "https://example.org/p")
    assert row["stable_digest"] == "FIRST"
    assert row["stable_bytes"] == 10


def test_a_digest_from_another_method_is_replaced_rather_than_compared(
    tmp_path,
) -> None:
    """The other half of the write-once rule, and the reason it was widened.

    A digest cut by a different method is not evidence about this one, so the
    write-once guard must NOT hold it in place -- if it did, every row baselined
    before 0.53.0 would keep an incomparable digest forever and report drift on
    every pass. Both directions: a different tag replaces, the same tag does not.
    """
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    assert set_stable_digest(
        db, "https://example.org/p", digest="OLDWAY", covers_bytes=10, algo="lines/0"
    )
    assert set_stable_digest(
        db, "https://example.org/p", digest="NEWWAY", covers_bytes=20,
        algo=snap.STABLE_ALGO,
    )
    row = row_for_url(db, "https://example.org/p")
    assert row["stable_digest"] == "NEWWAY"
    assert row["stable_algo"] == snap.STABLE_ALGO

    assert not set_stable_digest(
        db, "https://example.org/p", digest="AGAIN", covers_bytes=30,
        algo=snap.STABLE_ALGO,
    )
    assert row_for_url(db, "https://example.org/p")["stable_digest"] == "NEWWAY"


def test_too_many_units_records_nothing_rather_than_a_partial_list() -> None:
    d = snap._ChunkDigester(max_units=3)
    d.update(b"".join(bytes([i % 251]) for i in range(20_000)))
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
        sketch="", sketch_algo="",
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
            units=_units(text),
        )

    return hasher


def _run(db, hasher):
    return verify(db, hasher=hasher, clock=lambda: "NOW")


def test_a_digest_from_an_older_method_re_baselines_and_is_not_called_a_change(
    tmp_path,
) -> None:
    """The release-safety property, and the reason `stable_algo` exists.

    0.53.0 changed the unit from the line to a content-defined chunk, so every
    digest stored before it mismatches today's. Compared blindly that is 1,217
    rows reporting that their sources drifted on the day WE changed -- the tool
    manufacturing the exact class of finding it exists to report truthfully,
    which is the defect this project has now shipped and fixed for `unreachable`,
    `gone` and `blocked`.

    Both directions, because "not stable_changed" alone would pass on a verify
    that had stopped concluding anything: the row must come out re-baselined,
    carrying today's tag and today's digest.
    """
    url = "https://example.org/p"
    db = _seed(tmp_path / "i.db", url)
    # ALGO IS THE EMPTY STRING, because that is the tag every row written
    # before 0.53.0 actually carries -- the column's DEFAULT.
    #
    # The first version of this test seeded "lines/0", a value production has
    # never held, and it passed against a guard written as
    # `row["stable_algo"] and row["stable_algo"] != STABLE_ALGO` -- which is
    # False for '' and so could not fire for a single one of the 1,217 rows it
    # existed for. Five live rows were reported as DOCUMENT CHANGED before the
    # fixture was corrected. A fixture holding a value the production reader
    # never sees is not a test of the production reader, and this project has
    # now shipped that same shape three times (the frozen clock, `init_db`,
    # and here).
    assert set_stable_digest(db, url, digest="CUT_BY_LINES", covers_bytes=10, algo="")

    result = _run(db, _volatile_hasher([_page(1), _page(2)]))

    row = row_for_url(db, url)
    assert row["verify_outcome"] == snap.STABLE_REBASELINED
    assert result.stable_rebaselined == 1
    assert result.stable_changed == 0
    assert row["stable_algo"] == snap.STABLE_ALGO
    assert row["stable_digest"] != "CUT_BY_LINES"


def test_a_row_with_no_digest_at_all_baselines_rather_than_re_baselining(
    tmp_path,
) -> None:
    """The order of the two branches, asserted.

    A row that has never been characterised also has an empty `stable_algo`, so
    a re-baseline check written before the has-a-digest check would claim every
    first look was a re-cut. Both live on the empty string; only the digest
    tells them apart.
    """
    url = "https://example.org/p"
    db = _seed(tmp_path / "i.db", url)
    assert row_for_url(db, url)["stable_algo"] == ""
    assert row_for_url(db, url)["stable_digest"] == ""

    result = _run(db, _volatile_hasher([_page(1), _page(2)]))
    assert result.stable_baseline == 1
    assert result.stable_rebaselined == 0
    assert row_for_url(db, url)["verify_outcome"] == STABLE_BASELINE


def test_a_stored_digest_with_the_current_tag_is_still_compared(tmp_path) -> None:
    """The control for the test above: re-baselining must not swallow a real
    change. Same shape, same code path, only the stored tag differs."""
    url = "https://example.org/p"
    db = _seed(tmp_path / "i.db", url)
    baseline = _run(db, _volatile_hasher([_page(1), _page(2)]))
    assert baseline.stable_baseline == 1

    result = _run(db, _volatile_hasher([_replaced(8), _replaced(9)]))
    assert result.stable_changed == 1
    assert result.stable_rebaselined == 0
    assert row_for_url(db, url)["verify_outcome"] == STABLE_CHANGED


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

    result = _run(db, _volatile_hasher([_replaced(5), _replaced(6)]))

    row = row_for_url(db, "https://example.org/p")
    assert row["verify_outcome"] == STABLE_CHANGED
    assert result.stable_changed == 1
    # A finding is not erased by the pass that made it.
    assert row["stable_digest"] == before


def test_a_page_whose_units_do_not_align_stays_unstable_and_stores_nothing(
    tmp_path,
) -> None:
    """`verify` records nothing when the digest declines.

    The fixture hands it ONE unit holding the whole document, which is what a
    minified page produced when the unit was the line. That is no longer how a
    minified page is cut -- see `test_content_defined_units.py` -- so this is
    now a test of the refusal path itself rather than of that page.
    """
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
            units=_units(_page(1)),
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


# --- the domain moves between passes ------------------------------------------

# A page wide enough that one moved element is a realistic fraction of it. The
# fixtures above are 22 lines, so a single moved unit would be 4% of the
# document -- the live case is 46 chunks of 3,475, which is 1.3%.
WIDE = (
    ["<html>"]
    + [f"<p>paragraph {i} of the cited work</p>" for i in range(200)]
    + ["</html>"]
)
# Volatile, but SLOWER than the pair. springer stamps one of these into every
# reference anchor; it is fixed within a render and different across renders, so
# two reads seconds apart routinely agree on it and it is admitted as content.
SLOW = "<a id=ref-link-section-{}>16</a>"


def _render(nonce: int, render: str):
    return WIDE[:1] + [NONCE.format(nonce), SLOW.format(render)] + WIDE[1:]


def test_a_later_look_whose_agreed_set_moved_is_not_called_changed(
    tmp_path,
) -> None:
    """The defect, end to end.

    Both passes see the same document. Each pair strips its own per-request
    nonce, but each ADMITS the slower render id, because that id does not move
    within the two seconds between the reads. So the two passes fingerprint
    sets that differ by one element out of 202, and equality called that
    `stable_changed` -- 175 rows of it in the live index, 41 of them nature.com
    ARTICLES, which do not change.

    A verdict must not depend on which bytes the read-pair happened to agree on.
    """
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    _run(db, _volatile_hasher([_render(1, "a"), _render(2, "a")]))

    result = _run(db, _volatile_hasher([_render(3, "b"), _render(4, "b")]))

    row = row_for_url(db, "https://example.org/p")
    assert row["verify_outcome"] == STABLE_UNCHANGED
    assert result.stable_unchanged == 1
    assert result.stable_changed == 0


def test_a_document_that_really_changed_is_still_called_changed(tmp_path) -> None:
    """The positive control for the test above, in the same shape.

    Tolerating a moved domain must not become tolerating anything. Here the
    document itself is rewritten while the render id moves exactly as before, so
    the only difference from the test above is the content -- which is the one
    thing a provenance check exists to notice.
    """
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    _run(db, _volatile_hasher([_render(1, "a"), _render(2, "a")]))

    rewritten = (
        ["<html>"]
        + [f"<p>ENTIRELY DIFFERENT TEXT {i}</p>" for i in range(200)]
        + ["</html>"]
    )

    def _other(nonce: int, render: str):
        return rewritten[:1] + [NONCE.format(nonce), SLOW.format(render)] + rewritten[1:]

    result = _run(db, _volatile_hasher([_other(3, "b"), _other(4, "b")]))

    row = row_for_url(db, "https://example.org/p")
    assert row["verify_outcome"] == STABLE_CHANGED
    assert result.stable_changed == 1


def test_an_edit_smaller_than_the_pair_noise_is_not_reported(tmp_path) -> None:
    """The measured cost of the method, asserted so it cannot be forgotten.

    One paragraph of 22 is rewritten -- a REAL change -- and this reports
    `stable_unchanged`. That is not a threshold that wants tuning: unchanged
    pages measured 0.958 to 1.000 across 120 seconds, while rewriting 1,000
    bytes of a 271 KB article measured 0.990. The small edit sits INSIDE the
    band that unchanged pages occupy, so no threshold separates them and
    raising this one only converts the miss into false reports of drift.

    What was lost is smaller than it looks: the equality test this replaced
    reported `changed` for unchanged pages too, so it never distinguished this
    edit from noise either -- it just always said "changed" and was right by
    accident here.

    Detecting an edit this small needs a different measurement (more reads, over
    a longer span, to identify the volatile chunks properly), not a different
    number.
    """
    db = _seed(tmp_path / "i.db", "https://example.org/p")
    _run(db, _volatile_hasher([_page(1), _page(2)]))

    result = _run(db, _volatile_hasher([_edited(5), _edited(6)]))

    assert result.stable_changed == 0
    assert result.stable_unchanged == 1
