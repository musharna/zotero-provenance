"""A document is cut where its CONTENT says, not where its formatter did.

`stable_digest` compares two reads of a page and hashes what they agree on, so a
per-request token stops reading as provenance drift. It aligned LINES, and a
line's length is chosen by whoever formatted the source -- so the source decided
how much one nonce could cost us, and the answer was sometimes the whole page.

Measured 2026-09-03 against the live index, fetching each row twice three
seconds apart and scoring the result against what actually differs:

    huggingface.co/datasets/google/frames-benchmark   ONE line of 630 KB
                                                      0.05% truly different
                                                      -> coverage 0.65, refused
    link.springer.com/article/10.1186/s13059-...      81 per-render anchor ids,
                                                      ~9 bytes each = 0.12%
                                                      -> coverage 0.70, refused

Springer stamps a per-render number into every reference anchor; because those
anchors sit inside one 105,082-byte line, 111,300 bytes were forfeited for 729
that had moved. Over 38 live read-pairs the line unit refused 21 documents it
could have characterised. The content-defined unit refuses 1, a genuine
borderline at 3.95% truly different, and wrongly accepts none.

The unit is cut on a rolling hash of the bytes themselves, which is how rsync,
borg and git packfiles compare two versions of a stream. That is not a synonym
for "smaller units", and `test_fixed_size_blocks_are_the_control...` below is
the evidence: fixed 64-byte blocks are the same size and fall apart, because an
edit that changes a document's LENGTH shifts every block after it and they never
re-align. A content-defined boundary re-synchronises at the next hash hit.

MIN_STABLE_COVERAGE did not move. Everything characterisable scores 0.92 or
better and everything genuinely volatile 0.81 or worse -- a yahoo news page
rebuilt 22% of itself between two reads -- so the floor still sits in the gap it
was chosen for. That is the evidence that the unit was the defect and the
threshold was not.
"""

from __future__ import annotations

import hashlib

import zotero_capture.snapshot as snap
from zotero_capture.snapshot import CHUNK_MAX, PageRead, UnitRead, stable_digest

TOKEN = b"<meta name=csrf content=%s>"


def _one_enormous_line(nonce: bytes) -> bytes:
    """A realistic minified page: no newlines, one per-request token inside.

    The shape is taken from the measurement, not invented: the huggingface
    dataset page is a single 630 KB line and springer's volatile bytes sit
    inside a 105 KB one.
    """
    filler = b"".join(
        b"<p>paragraph %d of the cited work</p>" % i for i in range(4000)
    )
    half = len(filler) // 2
    return filler[:half] + TOKEN % nonce + filler[half:]


def _units(body: bytes) -> tuple[UnitRead, ...]:
    d = snap._ChunkDigester()
    d.update(body)
    got = d.finish()
    assert got is not None
    return got


def _as_lines(body: bytes) -> tuple[UnitRead, ...]:
    """The pre-0.53.0 unit, kept as a control rather than described in prose."""
    parts = body.split(b"\n")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return tuple(UnitRead(hashlib.sha256(x).hexdigest(), len(x)) for x in parts)


def _fixed(body: bytes, size: int = 64) -> tuple[UnitRead, ...]:
    """Same average size as the real unit, boundaries chosen by POSITION."""
    return tuple(
        UnitRead(hashlib.sha256(body[i : i + size]).hexdigest(), len(body[i : i + size]))
        for i in range(0, len(body), size)
    )


def _read(units: tuple[UnitRead, ...]) -> PageRead:
    return PageRead(
        digest="D",
        final_url="https://example.org/p",
        covers_bytes=sum(u.nbytes for u in units),
        complete=True,
        units=units,
    )


def test_a_nonce_inside_one_enormous_line_no_longer_vetoes_the_document() -> None:
    """The defect and the fix in one test, with the old unit as the control.

    Without the control this asserts only that the new cut works, which a
    function that always returned a digest would also satisfy. The control is
    what shows the input really is the shape that used to be refused.
    """
    a, b = _one_enormous_line(b"aaa1"), _one_enormous_line(b"bbb2")

    assert stable_digest(_read(_as_lines(a)), _read(_as_lines(b))) is None

    got = stable_digest(_read(_units(a)), _read(_units(b)))
    assert got is not None
    assert got.covers_bytes / len(a) > 0.99


def test_what_a_nonce_costs_is_bounded_by_the_unit_not_by_the_source() -> None:
    """The mechanism, stated as a number rather than as a verdict.

    A differing unit still forfeits its whole length -- that did not go away.
    What went away is the source's ability to decide what that length is.
    """
    a, b = _one_enormous_line(b"aaa1"), _one_enormous_line(b"bbb2")

    lost_by_lines = len(a) - 0  # the single line is forfeited entire
    assert len(_as_lines(a)) == 1, "fixture must be the shape that was refused"

    got = stable_digest(_read(_units(a)), _read(_units(b)))
    assert got is not None
    lost = len(a) - got.covers_bytes
    assert lost <= 4 * CHUNK_MAX, lost
    assert lost < lost_by_lines / 50


def test_no_source_formatting_can_make_a_unit_arbitrarily_large() -> None:
    """The property that makes this a mechanism removal and not a tripwire.

    There is no longer any value a source controls that grows a unit without
    bound: a document with no newline at all is still cut, and cut often.
    """
    body = _one_enormous_line(b"x")
    assert b"\n" not in body

    units = _units(body)
    # An ABSOLUTE bound, not `<= CHUNK_MAX`. Asserting a unit is no bigger than
    # the constant that defines its size is a tautology: mutation-tested by
    # setting CHUNK_MAX to 10**9, where the self-referential form still passed.
    assert CHUNK_MAX <= 4096
    assert max(u.nbytes for u in units) <= 4096
    assert len(units) > 100
    assert sum(u.nbytes for u in units) == len(body), "the document must survive intact"


def test_fixed_size_blocks_are_the_control_that_earns_content_defined_ones()  -> None:
    """Smaller units are NOT the fix; re-synchronising boundaries are.

    Fixed blocks here are the SAME 64-byte average as the real unit. An edit
    that changes the document's length shifts every block after it, so they
    never re-align -- measured live at 0.9966 coverage collapsing to 0.1881 on
    one page. Without this control, "we made the units smaller" would look like
    a sufficient account of the fix, and the next person would tune the size.
    """
    a = _one_enormous_line(b"short")
    b = _one_enormous_line(b"a_considerably_longer_token_value")

    fixed = stable_digest(_read(_fixed(a)), _read(_fixed(b)))
    content = stable_digest(_read(_units(a)), _read(_units(b)))

    assert content is not None
    # 0.961 measured. Lower than the same-length case above (0.997) because
    # re-synchronising after a length change costs the unit the edit lands in
    # plus the one that follows it -- bounded, and still far above the floor.
    assert content.covers_bytes / len(a) > 0.95
    assert fixed is None, "fixed blocks should lose alignment after a length change"


def test_a_page_that_genuinely_rebuilds_itself_is_still_refused() -> None:
    """The positive control for the refusal.

    Every test above widens what gets a digest. A function that had simply
    stopped declining would pass all of them. Live, this is the yahoo news class
    -- 22% of the bytes actually different between two reads seconds apart.
    """
    a = b"".join(b"<p>story %d as it stood</p>" % i for i in range(2000))
    b = b"".join(b"<p>story %d rewritten entirely</p>" % (i * 7) for i in range(2000))
    assert stable_digest(_read(_units(a)), _read(_units(b))) is None


def test_the_same_bytes_are_always_cut_the_same_way() -> None:
    """The digest is only comparable across passes if the cut is reproducible.

    The byte table is derived from sha256 rather than a seeded PRNG for this
    reason: a table that depended on an interpreter's random implementation
    would re-cut every document on an upgrade and report every source in the
    library as changed.
    """
    body = _one_enormous_line(b"stable")
    assert _units(body) == _units(body)
    assert snap._GEAR[0] == int.from_bytes(
        hashlib.sha256(bytes([0])).digest()[:8], "big"
    )
