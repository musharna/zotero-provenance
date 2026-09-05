"""A `changed` verdict should say HOW MUCH changed, not merely that it did.

710 of the 714 rows carrying `changed` have no stable digest: they read
identically twice 2s apart, so `verify` never enters the stable path and the
verdict rests entirely on a whole-document hash taken weeks earlier. Measured
2026-09-05 by reading 14 of them as two pairs 20 minutes apart:

    4help.vt.edu      a CSRF token whose TTL sits BETWEEN 2s and 20 min
    www.nature.com    ad cache-busters, c=-303483026 -> c=1158063100
    code.claude.com   activeDeploymentHistoryId -- a redeploy
    doi.org           "Views: 308" -> "Views: 310", a view COUNTER

4 of 14 differ over 20 minutes with no possible content change, so at least
that fraction of `changed` is manufactured. But 9 held steady, so -- unlike
`unstable` and `stable_changed` -- most of this bucket survived the probe.
Suppressing the verdict would trade a visible false positive for a silent false
negative, which for a link-rot tool is the worse direction.

So the whole-document hash KEEPS its exact sensitivity, and a sketch is stored
BESIDE it to give the mismatch a magnitude: a view counter and a rewritten
paragraph stop looking identical.

The sketch samples ALL chunks of one read, while `stable_digest` samples the
subset two reads agreed on. Different populations, so they carry different
tags and nothing may compare one with the other -- that confusion is exactly
what 0.55.0 removed.
"""

from __future__ import annotations

import datetime
import hashlib

from zotero_capture.snapshot import CONTENT_SKETCH_ALGO, PageRead, sketch_of, verify
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    row_for_url,
    set_content_hash,
)

URL = "https://example.org/p"

DOC = ["<html>"] + [f"<p>paragraph {i} of the cited work</p>" for i in range(200)] + ["</html>"]
COUNTER = "<p>Views: {}</p>"


def _units(lines):
    return tuple(
        __import__("zotero_capture.snapshot", fromlist=["UnitRead"]).UnitRead(
            hashlib.sha256(x.encode()).hexdigest(), len(x)
        )
        for x in lines
    )


def _read(lines) -> PageRead:
    return PageRead(
        digest=hashlib.sha256("".join(lines).encode()).hexdigest(),
        final_url=URL,
        covers_bytes=sum(len(x) for x in lines),
        complete=True,
        units=_units(lines),
    )


def _with_counter(n: int):
    return DOC[:1] + [COUNTER.format(n)] + DOC[1:]


def _seed(db, lines, *, sketch=None, algo=CONTENT_SKETCH_ALGO):
    init_db(db)
    insert_url(db, URL, "K1", datetime.date(2026, 5, 5))
    read = _read(lines)
    set_content_hash(
        db,
        URL,
        content_hash=read.digest,
        hashed_at="T1",
        covers_bytes=read.covers_bytes,
        complete=True,
        sketch=sketch_of(u.digest for u in read.units) if sketch is None else sketch,
        sketch_algo=algo,
    )
    return db


def _steady_hasher(lines):
    """A page that reads the SAME way twice -- the shape that produces `changed`."""

    def hasher(url: str, max_bytes: int) -> PageRead:
        return _read(lines)

    return hasher


def test_a_view_counter_is_reported_as_a_change_of_measurable_size(tmp_path) -> None:
    """The finding, end to end.

    The document is untouched; one counter incremented. The whole-document hash
    differs, so the verdict is still `changed` -- sensitivity is not traded away
    -- but it now carries the overlap, which is what separates this from a
    rewritten source.
    """
    db = _seed(tmp_path / "i.db", _with_counter(308))

    result = verify(
        db, hasher=_steady_hasher(_with_counter(310)), clock=lambda: "NOW"
    )

    assert result.changed == 1
    assert result.changed_similarity[URL] is not None
    assert result.changed_similarity[URL] > 0.9, (
        "one moved counter must read as a near-total overlap, not as a rewrite"
    )


def test_a_rewritten_document_scores_far_below_a_moved_counter(tmp_path) -> None:
    """Positive control. Every assertion above says a change reads as SMALL; a
    function returning 1.0 would satisfy all of them. Here the document really
    is replaced, and the same number has to collapse."""
    db = _seed(tmp_path / "i.db", _with_counter(308))
    other = ["<html>"] + [f"<p>an entirely different sentence {i}</p>" for i in range(200)] + ["</html>"]

    result = verify(db, hasher=_steady_hasher(other), clock=lambda: "NOW")

    assert result.changed == 1
    assert result.changed_similarity[URL] < 0.5


def test_a_sketch_cut_by_another_method_yields_no_opinion(tmp_path) -> None:
    """The 0.53.0 lesson, one column along: a value cut differently is not
    weaker evidence, it is none. Reporting it as a low overlap would announce a
    rewrite on the strength of a release of ours."""
    db = _seed(tmp_path / "i.db", _with_counter(308), algo="cdc64+kmv128/1")

    result = verify(db, hasher=_steady_hasher(_with_counter(310)), clock=lambda: "NOW")

    assert result.changed == 1
    assert result.changed_similarity[URL] is None


def test_a_row_with_no_sketch_yields_no_opinion(tmp_path) -> None:
    """True of every row hashed before this column existed. `None` must stay
    distinguishable from 0.0: one is "we cannot say", the other is "it was
    replaced", and rendering the first as the second manufactures a finding."""
    db = _seed(tmp_path / "i.db", _with_counter(308), sketch="", algo="")

    result = verify(db, hasher=_steady_hasher(_with_counter(310)), clock=lambda: "NOW")

    assert result.changed == 1
    assert result.changed_similarity[URL] is None


def test_a_document_that_still_matches_its_hash_acquires_a_sketch(tmp_path) -> None:
    """The only honest backfill.

    3,813 rows were hashed before this column existed. A sketch cannot be
    invented for them from a later read -- that would describe different bytes
    than the hash beside it. But when a fresh read still EQUALS `content_hash`,
    the bytes are proven identical, so a sketch cut now is a truthful sketch of
    what was hashed then. That equality is the entire licence.
    """
    db = _seed(tmp_path / "i.db", _with_counter(308), sketch="", algo="")
    assert row_for_url(db, URL)["content_sketch"] == ""

    verify(db, hasher=_steady_hasher(_with_counter(308)), clock=lambda: "NOW")

    row = row_for_url(db, URL)
    assert row["content_sketch"] != ""
    assert row["content_sketch_algo"] == CONTENT_SKETCH_ALGO


def test_a_document_that_differs_never_acquires_one(tmp_path) -> None:
    """The other direction, and the one that matters.

    Without this, the backfill above is satisfied by a version that writes a
    sketch on every path -- which would attach today's bytes to an August hash
    and make the two permanently incomparable while LOOKING repaired. Those
    rows' sketches are simply unrecoverable: only a digest was ever kept.
    """
    db = _seed(tmp_path / "i.db", _with_counter(308), sketch="", algo="")

    verify(db, hasher=_steady_hasher(_with_counter(310)), clock=lambda: "NOW")

    row = row_for_url(db, URL)
    assert row["content_sketch"] == ""
    assert row["content_sketch_algo"] == ""


def test_the_hashing_pass_stores_a_sketch_of_the_read_it_hashed(tmp_path) -> None:
    """The write path, end to end, driving `snapshot` rather than the helper.

    `verify` can only ever backfill; the sketch that matters is the one taken at
    the same instant as the hash. A test that only exercised the backfill would
    pass on a `snapshot` that stored nothing, leaving every NEW row exactly as
    uncharacterisable as the 3,813 old ones.
    """
    from zotero_capture.snapshot import snapshot

    db = tmp_path / "i.db"
    init_db(db)
    insert_url(db, URL, "K1", datetime.date(2026, 5, 5))
    lines = _with_counter(308)

    class _Zot:
        def record_content_hash(self, key, digest, *, expect_url):
            return True

    snapshot(
        db,
        zotero=_Zot(),
        hasher=lambda url, max_bytes: _read(lines),
        clock=lambda: "NOW",
        visible=lambda url: True,
    )

    row = row_for_url(db, URL)
    assert row["content_hash"] == _read(lines).digest
    assert row["content_sketch_algo"] == CONTENT_SKETCH_ALGO
    assert row["content_sketch"] == sketch_of(u.digest for u in _read(lines).units)


def test_the_report_says_it_cannot_size_a_change_rather_than_printing_zero(tmp_path) -> None:
    """A missing sketch must not render as a total rewrite.

    0.0 and None occupy the same slot in the report, and the whole history of
    this project's withdrawn findings is a confident value standing in for an
    absent one.
    """
    from zotero_capture.snapshot import format_verify_report

    db = _seed(tmp_path / "i.db", _with_counter(308), sketch="", algo="")
    result = verify(db, hasher=_steady_hasher(_with_counter(310)), clock=lambda: "NOW")

    changed_line = next(x for x in format_verify_report(result) if "CHANGED:" in x)
    assert "size unknown" in changed_line
    assert "0.000" not in changed_line


def test_a_read_that_recorded_no_units_yields_no_opinion(tmp_path) -> None:
    """The OTHER half of the guard, which the tests above never reached.

    They seed a row with no algo tag, so `_characterise` returns at the tag
    check and the empty-sketch branch is never executed -- two tests exercising
    one path, which a mutation making that branch return 0.0 survived. This
    drives the reachable half: `_ChunkDigester` records NOTHING when a document
    overflows `max_units`, so a live read really can arrive with `units is
    None`, and the stored sketch is fine. The answer must still be "cannot say".
    """
    db = _seed(tmp_path / "i.db", _with_counter(308))

    def _no_units(url: str, max_bytes: int) -> PageRead:
        moved = _with_counter(310)
        return PageRead(
            digest=hashlib.sha256("".join(moved).encode()).hexdigest(),
            final_url=url,
            covers_bytes=sum(len(x) for x in moved),
            complete=True,
            units=None,
        )

    result = verify(db, hasher=_no_units, clock=lambda: "NOW")

    assert result.changed == 1
    assert result.changed_similarity[URL] is None


def test_a_sketch_already_stored_is_never_replaced_by_a_later_read(tmp_path) -> None:
    """Write-once, asserted where a reader looks for it: through `verify`.

    The stored sketch is evidence of the bytes that were hashed. A pass that
    overwrote it with a later read would erase that evidence exactly as
    overwriting `content_hash` would -- and `verify` has never been allowed to
    touch that, for the same reason.
    """
    db = _seed(tmp_path / "i.db", _with_counter(308))
    before = row_for_url(db, URL)["content_sketch"]
    assert before != ""

    verify(db, hasher=_steady_hasher(_with_counter(308)), clock=lambda: "NOW")

    assert row_for_url(db, URL)["content_sketch"] == before


def test_a_total_overlap_does_not_read_as_an_unchanged_document(tmp_path) -> None:
    """The number and the verdict must not contradict each other in print.

    A sketch is 128 chunk digests, not the document: measured live, a page with
    11 differing chunks of 2,011 scored exactly 1.0 because none fell inside the
    window. Printing "1.000 of the document unchanged" next to CHANGED invites
    the reader to believe the friendlier half, which is how an overclaiming
    description already cost this project a whole class of unrepaired rows.
    """
    from zotero_capture.snapshot import _overlap_note

    note = _overlap_note(1.0)
    assert "document unchanged" not in note
    assert "resolve" in note
    assert "0.712 of the sampled chunks shared" in _overlap_note(0.712)
