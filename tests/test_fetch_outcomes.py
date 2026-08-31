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
    PageRead,
    TooLarge,
    classify_failure,
    responding_url,
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
    assert classify_failure(TooLarge("huge", final_url="")) == TOO_LARGE


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

    def hasher(url: str) -> PageRead:
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

    _run(db, lambda url: PageRead(digest=DIGEST, final_url=url))

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

    def hasher(url: str) -> PageRead:
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
    _run(db, lambda url: PageRead(digest=DIGEST, final_url=url))

    assert rows_needing_hash(db) == []
    assert rows_needing_hash(db, include_failed=True) == []


def test_the_limit_is_no_longer_blocked_by_dead_rows(db: Path) -> None:
    """The observed symptom: two consecutive --limit 5 runs examined the same
    dead rows and made no progress at all."""
    for i in range(3):
        insert_url(db, f"https://fixturehost.org/dead{i}", f"K{i}", date(2026, 5, 5))
    insert_url(db, "https://fixturehost.org/live", "K9", date(2026, 5, 9))

    calls: list[str] = []

    def hasher(url: str) -> PageRead:
        calls.append(url)
        if "dead" in url:
            raise _status(404)
        return PageRead(digest=DIGEST, final_url=url)

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

    def hasher(url: str) -> PageRead:
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


# --- narrowing a re-run to one host -----------------------------------------
#
# A fix that changes how ONE host answers should be provable on that host before
# it is let loose on 800 rows. `only_outcome` narrows on the classification;
# `only_host` narrows on the address. What makes it worth testing is the
# BOUNDARY: this project has shipped a substring test where a token was meant
# three times now (URL_RE as a character blacklist; an alert filter that read
# "OOM" out of "Bloomberg"), so most of the cases below are about what must NOT
# match rather than what must.


def _host_corpus(db: Path) -> None:
    for i, url in enumerate(
        (
            "https://wikipedia.org/wiki/A",
            "https://en.wikipedia.org/wiki/B",
            "http://de.wikipedia.org/wiki/D",
            "https://notwikipedia.org/wiki/E",
            "https://wikipedia.org.evil.test/wiki/F",
            "https://example.org/wikipedia.org/G",
        )
    ):
        insert_url(db, url, f"KEY{i}", date(2026, 5, 5))


def _urls(rows) -> set[str]:
    return {r["url_canonical"] for r in rows}


def test_only_host_takes_the_host_and_its_subdomains(db: Path) -> None:
    """The positive control for every refusal below: a filter that matched
    nothing would satisfy all of them and make the tool do no work at all."""
    _host_corpus(db)
    assert _urls(rows_needing_hash(db, only_host="wikipedia.org")) == {
        "https://wikipedia.org/wiki/A",
        "https://en.wikipedia.org/wiki/B",
        "http://de.wikipedia.org/wiki/D",
    }


def test_only_host_refuses_a_host_that_merely_ends_the_same_way(db: Path) -> None:
    """notwikipedia.org is a different site. A suffix test without the dot takes
    it, and the run then reaches a host nobody authorised."""
    _host_corpus(db)
    assert "https://notwikipedia.org/wiki/E" not in _urls(
        rows_needing_hash(db, only_host="wikipedia.org")
    )


def test_only_host_refuses_a_host_that_merely_starts_the_same_way(db: Path) -> None:
    """The right-hand anchor. wikipedia.org.evil.test belongs to evil.test, and a
    prefix match hands it every request the filter was meant to confine."""
    _host_corpus(db)
    assert "https://wikipedia.org.evil.test/wiki/F" not in _urls(
        rows_needing_hash(db, only_host="wikipedia.org")
    )


def test_only_host_does_not_match_the_name_inside_a_path(db: Path) -> None:
    _host_corpus(db)
    assert "https://example.org/wikipedia.org/G" not in _urls(
        rows_needing_hash(db, only_host="wikipedia.org")
    )


def test_only_host_treats_a_like_wildcard_as_a_literal(db: Path) -> None:
    """'%' reaching LIKE unescaped would widen a scoped re-run to the whole
    corpus while the report still called it scoped -- a failure invisible in its
    own output, which is the kind this project keeps shipping."""
    _host_corpus(db)
    assert rows_needing_hash(db, only_host="%") == []
    assert rows_needing_hash(db, only_host="wikipedi%.org") == []
    assert rows_needing_hash(db, only_host="wikipedia_org") == []


def test_only_host_tolerates_harmless_spellings(db: Path) -> None:
    _host_corpus(db)
    for spelling in ("WIKIPEDIA.ORG", ".wikipedia.org", "  wikipedia.org  "):
        assert len(rows_needing_hash(db, only_host=spelling)) == 3, spelling


def test_only_host_refuses_an_empty_name(db: Path) -> None:
    """An empty filter that quietly meant "everything" is the widening bug in its
    most direct form: the caller asked to narrow and got the opposite."""
    _host_corpus(db)
    with pytest.raises(ValueError):
        rows_needing_hash(db, only_host="   ")


def test_only_host_composes_with_only_outcome(db: Path) -> None:
    """The two filters narrow on different axes and must intersect, not replace.
    Written because `only_outcome` sits in an if/elif with `include_failed`, and
    a third filter added into that chain would silently displace one of them."""
    from zotero_capture.sqlite_cache import set_fetch_outcome

    _host_corpus(db)
    set_fetch_outcome(
        db, "https://en.wikipedia.org/wiki/B", outcome=BLOCKED, at="T", final_url=""
    )
    set_fetch_outcome(
        db, "https://notwikipedia.org/wiki/E", outcome=BLOCKED, at="T", final_url=""
    )
    assert _urls(
        rows_needing_hash(db, only_host="wikipedia.org", only_outcome=BLOCKED)
    ) == {"https://en.wikipedia.org/wiki/B"}
