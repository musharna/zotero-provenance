"""A verdict must not depend on which bytes the read-pair happened to agree on.

The stable digest hashed the chunks that agreed within ONE pair of reads, and
`verify` then compared two such hashes for equality. Measured 2026-09-04 against
the live corpus, that produced `stable_changed` 175 against `stable_unchanged`
2 -- a corpus of published papers cannot be 99% drifted.

The proof it was ours: a nature.com article read twice, 120s apart. The two
bodies differed in 32 byte positions and they were one CSRF token; the article
itself was byte-identical. Yet the agreed list was 3,475 chunks in one round and
3,429 in the other, so the two hashes covered DIFFERENT REGIONS of the page.
Comparing them can only produce a verdict about us.

The domain cannot be pinned -- it is derived from whatever two reads disagree
on, which is the whole point of the method. So the comparison changes instead:
a bounded sketch of the agreed SET, compared by similarity rather than equality.
A page that gained 46 volatile chunks is then 98.7% similar, not "CHANGED".
"""

from __future__ import annotations

import hashlib

import zotero_capture.snapshot as snap


def _digests(start: int, n: int) -> list[str]:
    """n distinct chunk digests, shaped like the sha256 hex the chunker emits.

    Real sha256 hex, not a counter formatted to 64 places: the sketch keeps the
    LEADING bits, and `f"{i:064x}"` puts 48 zeros there, so every synthetic
    digest would collapse to the same value and the test would pass on a sketch
    that discarded everything. An unrealistic fixture makes a threshold test
    meaningless -- the 0.50.0 coverage fixture failed exactly this way.
    """
    return [hashlib.sha256(f"chunk-{start + i}".encode()).hexdigest() for i in range(n)]


def test_a_page_whose_volatile_bytes_moved_is_not_called_changed() -> None:
    """The measured case. 3,475 agreed chunks one pass, 3,429 the next, and the
    document between them was byte-identical bar a CSRF token."""
    shared = _digests(0, 3429)
    first = shared + _digests(9_000_000, 46)
    similarity = snap.stable_similarity(
        snap.sketch_of(first), snap.sketch_of(shared)
    )
    assert similarity >= snap.MIN_STABLE_SIMILARITY


def test_a_different_document_is_still_called_changed() -> None:
    """The positive control for the test above. A method that called everything
    unchanged would satisfy it, and would be useless."""
    similarity = snap.stable_similarity(
        snap.sketch_of(_digests(0, 3000)),
        snap.sketch_of(_digests(5_000_000, 3000)),
    )
    assert similarity < snap.MIN_STABLE_SIMILARITY


def test_the_fingerprint_does_not_depend_on_the_order_of_the_chunks() -> None:
    """The old digest folded the agreed units in document order, so identical
    content re-cut into a different sequence produced a different value. A set
    has no order, and the sketch of one must not either."""
    chunks = _digests(0, 500)
    assert snap.sketch_of(chunks) == snap.sketch_of(list(reversed(chunks)))


def test_the_fingerprint_does_not_depend_on_how_often_a_chunk_repeats() -> None:
    """Boilerplate repeats. Under the ordered fold, one extra copy of a chunk
    already in the set moved the value -- nature's unit count went 3,476 to
    3,478 across bodies of identical length."""
    chunks = _digests(0, 500)
    assert snap.sketch_of(chunks) == snap.sketch_of(chunks + chunks[:20])


def test_the_sketch_stays_small_however_large_the_document() -> None:
    """It is stored per row. Keeping every agreed digest for a 200,000-chunk
    document would put ~12 MB in one column."""
    assert len(snap.sketch_of(_digests(0, 200_000))) <= 4096


def test_a_set_compared_with_itself_is_wholly_similar() -> None:
    sketch = snap.sketch_of(_digests(0, 1000))
    assert snap.stable_similarity(sketch, sketch) == 1.0


def test_a_sketch_smaller_than_the_bound_is_exact() -> None:
    """Below the sketch size nothing is discarded, so the estimate is the real
    Jaccard and small documents get an exact answer rather than a sampled one."""
    a, b = _digests(0, 10), _digests(0, 5) + _digests(9_000, 5)
    assert snap.stable_similarity(snap.sketch_of(a), snap.sketch_of(b)) == 5 / 15


def test_an_unreadable_sketch_yields_no_opinion() -> None:
    """A row written by a method this code cannot parse must produce None --
    'no answer' -- never a similarity that would be read as a verdict."""
    assert snap.stable_similarity("not-a-sketch", snap.sketch_of(_digests(0, 10))) is None


def test_the_threshold_sits_between_the_two_measured_populations() -> None:
    """Both bounds are live measurements, not judgement.

    0.59479 is the most similar PAIR OF DIFFERENT ARTICLES found on one host
    (two github repo pages, which share a lot of chrome). 0.95824 is the LEAST
    similar reading of one unchanged document across 120 seconds (a springer
    article). A threshold outside that range is refuted by data already taken:
    above it, unchanged pages get reported as changed -- the defect this file
    removed; below it, two different papers compare as the same document.
    """
    assert 0.59479 < snap.MIN_STABLE_SIMILARITY < 0.95824


def test_a_small_real_edit_is_below_the_noise_and_that_is_stated() -> None:
    """The method's limit, asserted so it cannot be quietly forgotten.

    Rewriting 1,000 bytes of a 271 KB article measured 0.990 -- above the 0.958
    floor that unchanged pages sit at, so it cannot be told from noise. This is
    a property of comparing derived domains, not of the threshold, and the
    docstring says so rather than the number being tuned to hide it.
    """
    measured_small_edit = 0.990
    measured_noise_floor = 0.958
    assert measured_small_edit > measured_noise_floor
    assert measured_small_edit > snap.MIN_STABLE_SIMILARITY


def test_an_empty_sketch_yields_no_opinion() -> None:
    """`verify` cannot reach this -- a row holding no sketch takes the baseline
    branch instead -- so this drives the function directly.

    A guard the production reader cannot feed is a guard no test can fail on,
    which is the class the 0.22.0 audit found twice and a surviving mutation
    found again here. The behaviour matters because an empty set would compare
    as TOTAL AGREEMENT with another empty one, turning "we stored nothing" into
    `stable_unchanged`.
    """
    assert snap.stable_similarity("", snap.sketch_of(_digests(0, 10))) is None
    assert snap.stable_similarity(snap.sketch_of(_digests(0, 10)), "") is None
