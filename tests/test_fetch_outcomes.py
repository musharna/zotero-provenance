"""Why a page could not be read, kept as a fact rather than thrown away.

`snapshot` recorded every failure as "unreachable". That single word covered two
findings that mean opposite things:

  * **gone** (404/410) is the provenance finding the hash exists to produce. The
    citation no longer resolves; that is a real fact about the source.
  * **blocked** (401/403) says nothing whatsoever about the page. We were turned
    away at the door. The source may be perfectly intact.

Measured on the live corpus 2026-08-29: of 1,285 "unreachable" rows, whole hosts
failed at 100% -- academic.oup.com 87/87, pubmed 83/83, en.wikipedia.org 36/36,
sciencedirect 32/32 -- and probing them returned 403, while GitHub's failures
were genuine 404s. Link rot does not cluster per-host at exactly 100%. Collapsing
the two makes the library assert link rot that never happened.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from zotero_capture.snapshot import (
    BLOCKED,
    GONE,
    OK,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMEOUT,
    TOO_LARGE,
    UNREACHABLE,
    TooLarge,
    classify_failure,
    snapshot,
)
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    rows_needing_hash,
    row_for_url,
)

DIGEST = "d" * 64


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


class _Stamper:
    def record_content_hash(self, item_key, digest, *, expect_url=None):
        return True


def _status(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://fixturehost.org/a")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code}", request=request, response=response)


# --- the classifier ---------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        (404, GONE),
        (410, GONE),
        (401, BLOCKED),
        (403, BLOCKED),
        (429, RATE_LIMITED),
        (500, SERVER_ERROR),
        (503, SERVER_ERROR),
        (418, UNREACHABLE),
    ],
)
def test_a_status_is_classified_by_what_it_means(code: int, expected: str) -> None:
    assert classify_failure(_status(code)) == expected


def test_a_timeout_is_not_a_missing_page() -> None:
    assert classify_failure(httpx.ReadTimeout("slow")) == TIMEOUT


def test_a_connection_failure_is_unreachable() -> None:
    assert classify_failure(httpx.ConnectError("no route")) == UNREACHABLE


def test_an_oversized_page_keeps_its_own_outcome() -> None:
    """It was read successfully and refused deliberately; that is not a failure
    of the source."""
    assert classify_failure(TooLarge("huge")) == TOO_LARGE


def test_an_unknown_error_does_not_become_a_finding() -> None:
    """Anything unrecognised must fall to UNREACHABLE, never to GONE. Guessing
    'gone' from an error we do not understand would manufacture link rot."""
    assert classify_failure(RuntimeError("something new")) == UNREACHABLE


# --- THE property -----------------------------------------------------------


def _run(db: Path, hasher) -> None:
    snapshot(db, zotero=_Stamper(), hasher=hasher, clock=lambda: "NOW")


def test_blocked_and_gone_are_not_the_same_record(db: Path) -> None:
    """The whole point. Before this, both wrote 'unreachable' and the library
    could not tell a dead citation from a closed door."""
    insert_url(db, "https://fixturehost.org/blocked", "KEY1", date(2026, 5, 5))
    insert_url(db, "https://fixturehost.org/gone", "KEY2", date(2026, 5, 6))

    def hasher(url: str) -> str:
        raise _status(403 if url.endswith("/blocked") else 404)

    _run(db, hasher)

    assert row_for_url(db, "https://fixturehost.org/blocked")["last_outcome"] == BLOCKED
    assert row_for_url(db, "https://fixturehost.org/gone")["last_outcome"] == GONE


def test_a_failed_fetch_is_recorded_as_attempted(db: Path) -> None:
    """Closes the gap that made every pass re-fetch the same dead rows: absence
    of a hash meant 'never tried' and 'tried and failed' indistinguishably."""
    insert_url(db, "https://fixturehost.org/a", "KEY1", date(2026, 5, 5))

    _run(db, lambda url: (_ for _ in ()).throw(_status(403)))

    row = row_for_url(db, "https://fixturehost.org/a")
    assert row["last_attempt_at"] == "NOW"
    assert row["last_outcome"] == BLOCKED


def test_a_success_is_recorded_too(db: Path) -> None:
    """Positive control on the test above: if only failures were stamped, an
    empty last_outcome would be ambiguous between success and never-tried."""
    insert_url(db, "https://fixturehost.org/a", "KEY1", date(2026, 5, 5))

    _run(db, lambda url: DIGEST)

    row = row_for_url(db, "https://fixturehost.org/a")
    assert row["last_outcome"] == OK
    assert row["last_attempt_at"] == "NOW"
    assert row["content_hash"] == DIGEST


# --- the queue predicate ----------------------------------------------------


def test_a_recorded_failure_is_not_retried_by_default(db: Path) -> None:
    insert_url(db, "https://fixturehost.org/dead", "KEY1", date(2026, 5, 5))
    _run(db, lambda url: (_ for _ in ()).throw(_status(404)))

    assert [r["url_canonical"] for r in rows_needing_hash(db)] == []


def test_a_row_never_attempted_is_still_selected(db: Path) -> None:
    """Positive control. A predicate that excluded everything would satisfy the
    test above perfectly while making the tool do nothing at all."""
    insert_url(db, "https://fixturehost.org/dead", "KEY1", date(2026, 5, 5))
    insert_url(db, "https://fixturehost.org/fresh", "KEY2", date(2026, 5, 6))

    def hasher(url: str) -> str:
        raise _status(404)

    snapshot(db, zotero=_Stamper(), hasher=hasher, clock=lambda: "NOW", limit=1)

    assert [r["url_canonical"] for r in rows_needing_hash(db)] == [
        "https://fixturehost.org/fresh"
    ]


def test_retry_failed_reaches_them_again(db: Path) -> None:
    insert_url(db, "https://fixturehost.org/dead", "KEY1", date(2026, 5, 5))
    _run(db, lambda url: (_ for _ in ()).throw(_status(403)))

    again = rows_needing_hash(db, include_failed=True)
    assert [r["url_canonical"] for r in again] == ["https://fixturehost.org/dead"]


def test_a_hashed_row_is_never_selected_either_way(db: Path) -> None:
    """A retry pass must not re-read pages that already have their evidence."""
    insert_url(db, "https://fixturehost.org/a", "KEY1", date(2026, 5, 5))
    _run(db, lambda url: DIGEST)

    assert rows_needing_hash(db) == []
    assert rows_needing_hash(db, include_failed=True) == []


def test_the_limit_is_no_longer_blocked_by_dead_rows(db: Path) -> None:
    """The observed symptom: two consecutive --limit 5 runs examined the same
    dead rows and made no progress at all."""
    for i in range(3):
        insert_url(db, f"https://fixturehost.org/dead{i}", f"K{i}", date(2026, 5, 5))
    insert_url(db, "https://fixturehost.org/live", "K9", date(2026, 5, 9))

    calls: list[str] = []

    def hasher(url: str) -> str:
        calls.append(url)
        if "dead" in url:
            raise _status(404)
        return DIGEST

    snapshot(db, zotero=_Stamper(), hasher=hasher, clock=lambda: "NOW", limit=3)
    first = list(calls)
    snapshot(db, zotero=_Stamper(), hasher=hasher, clock=lambda: "NOW", limit=3)

    assert first == [f"https://fixturehost.org/dead{i}" for i in range(3)]
    assert calls[3:] == ["https://fixturehost.org/live"], "second pass repeated work"


# --- the report -------------------------------------------------------------


def test_the_result_breaks_failures_down(db: Path) -> None:
    """A run that says '1285 unreachable' hides that 500 of them were a closed
    door. The counts are what make the residue actionable."""
    insert_url(db, "https://fixturehost.org/blocked", "K1", date(2026, 5, 1))
    insert_url(db, "https://fixturehost.org/gone", "K2", date(2026, 5, 2))
    insert_url(db, "https://fixturehost.org/slow", "K3", date(2026, 5, 3))

    def hasher(url: str) -> str:
        if url.endswith("/blocked"):
            raise _status(403)
        if url.endswith("/gone"):
            raise _status(404)
        raise httpx.ReadTimeout("slow")

    result = snapshot(db, zotero=_Stamper(), hasher=hasher, clock=lambda: "NOW")

    assert result.by_outcome[BLOCKED] == 1
    assert result.by_outcome[GONE] == 1
    assert result.by_outcome[TIMEOUT] == 1
    assert result.unreachable == 3, "the existing total must not change meaning"
