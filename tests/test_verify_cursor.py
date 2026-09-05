"""A verify pass could not remember which rows it had already read.

`rows_with_hash` accepts `limit` and there is no OFFSET anywhere in the
codebase, so `--verify --limit 500` returns THE SAME first 500 rows on every
invocation. Verify recorded nothing either, so nothing else could tell them
apart. The pass was one all-or-nothing 2.7-hour run or a permanently
head-biased sample, and neither is a sweep.

This is not a missing OFFSET. The identical defect was diagnosed and fixed in
the OTHER fetching pass, and the migration that fixed it says so in as many
words (sqlite_cache.py, the `last_outcome` migration):

    Absence of a hash used to mean both "never attempted" and "attempted and
    failed", so every pass re-fetched the same dead rows forever and `--limit`
    never got past them.

`rows_needing_hash` therefore selects on recorded per-row state, and advances.
`rows_with_hash` selects on position, and cannot. So this is the same shape as
`_HostPacer`: a property of FETCHING that got built into `snapshot` and had to
be retrofitted to `verify` afterwards, because there was nothing to inherit.

An OFFSET would also have been positionally wrong. The order is
`hashed_at, url_canonical`, and ~1,353 live rows share one identical
`hashed_at` (the 0.34.0 constant-injection bug), while a concurrent snapshot
pass rewrites `hashed_at` under the reader. Paginating by position over an
ordering that moves is how chunk N+1 silently skips rows -- and a sweep that
skips rows reports a coverage number it did not earn.

The invariant that MUST survive: verify still never rewrites a stored hash.
Recording that we looked is not recording what we found.
"""

from __future__ import annotations

import datetime
import inspect

import httpx
import pytest

from zotero_capture.snapshot import PageRead, snapshot, verify
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    row_for_url,
    set_content_hash,
)

# Reused, not re-derived. This list comes from what a function DOES (it calls
# `hasher(...)`), and a second hand-written copy of it here would be the exact
# mistake the User-Agent guard made by naming its subjects.
from test_verify_pacing import _functions_that_fetch


def _ok(url: str, max_bytes: int = 0) -> PageRead:
    return PageRead(digest="OLD", final_url=url, covers_bytes=64, complete=True)


def _seed_hashed(db, urls):
    init_db(db)
    for i, url in enumerate(urls):
        insert_url(db, url, f"K{i}", datetime.date(2026, 5, 5))
        set_content_hash(
            db, url, content_hash="OLD", hashed_at=f"T{i}",
            covers_bytes=64, complete=True,
            sketch="", sketch_algo="",
        )
    return db


def _seed_unhashed(db, urls):
    init_db(db)
    for i, url in enumerate(urls):
        insert_url(db, url, f"K{i}", datetime.date(2026, 5, 5))
    return db


def _ticking():
    """Distinct stamps per row. A frozen clock would still pass the ordering
    tests here, and that is exactly how a batch constant hid for 1,348 rows."""
    n = [0]

    def clock() -> str:
        n[0] += 1
        return f"T{n[0]:03d}"

    return clock


class _Stamper:
    def record_content_hash(self, item_key, digest, *, expect_url=None):
        return True


# --- the defect ---------------------------------------------------------------


def test_a_limited_verify_run_does_not_re_read_the_same_rows(tmp_db) -> None:
    """The bug, stated directly. Two chunks of one must cover two rows."""
    db = _seed_hashed(tmp_db, ["https://a.test/1", "https://b.test/2"])
    seen: list[str] = []

    def watch(url: str, max_bytes: int = 0) -> PageRead:
        seen.append(url)
        return _ok(url)

    clock = _ticking()
    verify(db, hasher=watch, clock=clock, limit=1)
    verify(db, hasher=watch, clock=clock, limit=1)

    assert len(set(seen)) == 2, (
        f"two chunks read {seen} -- the second chunk re-read the first row, so "
        f"--limit can never advance past the head of the corpus"
    )


def test_a_chunked_sweep_covers_every_row_exactly_once(tmp_db) -> None:
    """Totality. Advancing is not enough if it advances past rows unread.

    The chunk size deliberately does not divide the corpus. 4 chunks of 2 over
    7 rows is 8 reads, and the 8th is NOT a bug: with nothing unverified left it
    begins the next sweep at the stalest row. Asserting `sorted(seen) == urls`
    would have failed on correct behaviour, so the boundary is asserted rather
    than avoided -- a round number would have hidden which of the two was
    happening.
    """
    urls = [f"https://h{i}.test/p" for i in range(7)]
    db = _seed_hashed(tmp_db, urls)
    seen: list[str] = []

    def watch(url: str, max_bytes: int = 0) -> PageRead:
        seen.append(url)
        return _ok(url)

    clock = _ticking()
    for _ in range(4):
        verify(db, hasher=watch, clock=clock, limit=2)

    assert sorted(seen[:7]) == sorted(urls), (
        f"a chunked sweep must read each row once; got {sorted(seen[:7])}"
    )
    assert seen[7] == seen[0], (
        "with the corpus swept, the next read must wrap to the stalest row"
    )


# --- the derived guard --------------------------------------------------------
#
# Every pass that fetches must advance under a limit. The pass list is derived
# from behaviour; the drivers are registered here, and a pass with no driver
# fails loudly rather than being silently skipped.


def _drive_snapshot(db, watch, limit):
    snapshot(db, zotero=_Stamper(), hasher=watch, clock=_ticking(), limit=limit)


def _drive_verify(db, watch, limit):
    verify(db, hasher=watch, clock=_ticking(), limit=limit)


_DRIVERS = {"snapshot": _drive_snapshot, "verify": _drive_verify}
_SEEDERS = {"snapshot": _seed_unhashed, "verify": _seed_hashed}


def test_every_fetching_pass_has_an_advance_driver() -> None:
    """Positive control, and the thing that fails when a THIRD pass is added.

    Without it the parametrised test below would simply not run for the new
    pass, and a guard that silently covers less than it claims is how the last
    three of these were missed."""
    found = set(_functions_that_fetch())
    assert found, "the derivation found no fetching passes at all"
    missing = found - set(_DRIVERS)
    assert not missing, (
        f"{sorted(missing)} fetch pages but have no resumability driver here; "
        f"add one rather than letting this guard quietly shrink"
    )


@pytest.mark.parametrize("name", sorted(_functions_that_fetch()))
def test_every_fetching_pass_advances_under_a_limit(name: str, tmp_db) -> None:
    urls = ["https://a.test/1", "https://b.test/2"]
    db = _SEEDERS[name](tmp_db, urls)
    seen: list[str] = []

    def watch(url: str, max_bytes: int = 0) -> PageRead:
        seen.append(url)
        return _ok(url)

    _DRIVERS[name](db, watch, 1)
    _DRIVERS[name](db, watch, 1)

    assert len(set(seen)) == 2, (
        f"{name}() with limit=1 read {seen} twice; a limited pass that cannot "
        f"advance can only ever see the head of the corpus"
    )


# --- what gets recorded -------------------------------------------------------


def test_a_verified_row_records_when_and_what(tmp_db) -> None:
    db = _seed_hashed(tmp_db, ["https://a.test/1"])
    verify(db, hasher=_ok, clock=lambda: "WHEN")

    row = row_for_url(db, "https://a.test/1")
    assert row["verified_at"] == "WHEN"
    assert row["verify_outcome"] == "unchanged"


def test_a_row_we_could_not_re_read_is_still_marked(tmp_db) -> None:
    """The head-blocking half. A paywall answered us -- that IS an observation,
    and leaving it unmarked is what made 1,285 dead rows re-fetch forever in the
    snapshot pass before 0.36.0."""
    db = _seed_hashed(tmp_db, ["https://a.test/1"])

    def refuse(url: str, max_bytes: int = 0):
        raise httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", url),
            response=httpx.Response(403, request=httpx.Request("GET", url)),
        )

    verify(db, hasher=refuse, clock=lambda: "WHEN")

    row = row_for_url(db, "https://a.test/1")
    assert row["verified_at"] == "WHEN"
    assert row["verify_outcome"] == "blocked"


def test_our_own_bug_marks_the_row_with_nothing(tmp_db) -> None:
    """Both directions. A TypeError in the hasher is a fact about US, and
    writing it onto the row would (a) claim a verdict nobody reached and
    (b) advance the cursor past a row that was never actually verified."""
    db = _seed_hashed(tmp_db, ["https://a.test/1"])

    def boom(url: str, max_bytes: int = 0):
        raise TypeError("our bug")

    result = verify(db, hasher=boom, clock=lambda: "WHEN")

    row = row_for_url(db, "https://a.test/1")
    assert result.internal_errors == 1
    assert row["verified_at"] == "", "our bug advanced the cursor"
    assert row["verify_outcome"] == "", "our bug was recorded as a verdict"


def test_verify_still_never_rewrites_the_stored_hash(tmp_db) -> None:
    """The invariant that survives making verify a writer. Recording that we
    LOOKED must not become recording what we FOUND -- the stored hash is the
    evidence of what was consulted."""
    db = _seed_hashed(tmp_db, ["https://a.test/1"])

    def moved(url: str, max_bytes: int = 0) -> PageRead:
        return PageRead(digest="NEW", final_url=url, covers_bytes=64, complete=True)

    verify(db, hasher=moved, clock=lambda: "WHEN")

    row = row_for_url(db, "https://a.test/1")
    assert row["content_hash"] == "OLD", "verify overwrote the evidence"
    assert row["hashed_at"] == "T0"
    assert row["verify_outcome"] == "changed"


# --- ordering -----------------------------------------------------------------


def test_the_never_verified_are_read_before_the_already_verified(tmp_db) -> None:
    db = _seed_hashed(tmp_db, ["https://a.test/1", "https://b.test/2"])
    verify(db, hasher=_ok, limit=1, clock=lambda: "T1")

    seen: list[str] = []

    def watch(url: str, max_bytes: int = 0) -> PageRead:
        seen.append(url)
        return _ok(url)

    verify(db, hasher=watch, limit=1, clock=lambda: "T2")
    assert seen == ["https://b.test/2"]


def test_a_second_sweep_re_reads_the_least_recently_verified_first(tmp_db) -> None:
    """A verify pass is not one-shot: a corpus fully swept once must become
    sweepable again, oldest first, with no flag and no reset."""
    db = _seed_hashed(tmp_db, ["https://a.test/1", "https://b.test/2"])
    verify(db, hasher=_ok, limit=1, clock=lambda: "T1")   # a.test
    verify(db, hasher=_ok, limit=1, clock=lambda: "T2")   # b.test

    seen: list[str] = []

    def watch(url: str, max_bytes: int = 0) -> PageRead:
        seen.append(url)
        return _ok(url)

    verify(db, hasher=watch, limit=1, clock=lambda: "T3")
    assert seen == ["https://a.test/1"], (
        "a second sweep must start with the stalest row, not the head of the "
        "table"
    )


def test_the_clock_is_read_per_row_not_per_run(tmp_db) -> None:
    """`hashed_at` stamped every page with the RUN's start time for 1,348 live
    rows because a batch constant is indistinguishable from a clock that never
    moves. A ticking fixture is the only shape that can tell them apart."""
    db = _seed_hashed(tmp_db, ["https://a.test/1", "https://b.test/2"])
    ticks = iter(["T1", "T2"])
    verify(db, hasher=_ok, clock=lambda: next(ticks))

    stamps = {
        row_for_url(db, u)["verified_at"]
        for u in ("https://a.test/1", "https://b.test/2")
    }
    assert stamps == {"T1", "T2"}, f"one timestamp for the whole run: {stamps}"


def test_verify_takes_a_clock() -> None:
    """It could not record WHEN before this, because it had no clock at all."""
    assert "clock" in inspect.signature(verify).parameters
