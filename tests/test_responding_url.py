"""An outcome is a fact about the host that ANSWERED, not the one we asked.

The live index recorded `blocked` against doi.org 177 times. doi.org had
answered every one of them correctly, with a 302; the 403 came from
academic.oup.com one hop later, and its name appeared nowhere in the row. So the
index blamed a resolver for a refusal it never made, and hid the party that did.

These tests drive REAL redirect chains through httpx rather than constructing the
exceptions by hand. A hand-built `HTTPStatusError` would carry whatever URL the
test chose to put on it, which would prove only that the assertion matches the
fixture -- the thing being tested is precisely whether httpx's redirect handling
and our reading of it agree, and a fake response cannot answer that.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest

from zotero_capture.snapshot import (
    BLOCKED,
    HASH_MAX_BYTES,
    OK,
    TOO_LARGE,
    PageRead,
    SnapshotResult,
    TooLarge,
    format_snapshot_report,
    hash_page,
    responding_url,
    snapshot,
)
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    row_for_url,
)

RESOLVER = "https://resolver.invalid/10.1093/some/doi"
PUBLISHER = "https://publisher.invalid/article/12345"
DIRECT = "https://publisher.invalid/direct"
BODY = b"the article"
SEEN = date(2026, 5, 5)


class _Stamper:
    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.stamped: list[tuple[str, str]] = []

    def record_content_hash(self, item_key, digest, *, expect_url=None):
        self.stamped.append((item_key, digest))
        return self.accepts


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


def _chain(*, status: int, body: bytes = BODY) -> httpx.Client:
    """A doi.org-shaped chain: the resolver 302s, the publisher answers.

    `follow_redirects=True` mirrors `build_fetch_client`, which is the client
    production actually hands to `hash_page`.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "resolver.invalid":
            return httpx.Response(302, headers={"Location": PUBLISHER})
        return httpx.Response(status, content=body)

    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )


def _hasher(http: httpx.Client):
    return lambda url: hash_page(url, client=http)


# --- reading the address off a failure ---------------------------------------


def test_a_refusal_after_a_redirect_names_the_host_that_refused() -> None:
    """THE property. Asserted against the requested URL too, because equality
    with the publisher and difference from the resolver are two claims and only
    the pair rules out a stub that returns its own input."""
    with _chain(status=403) as http:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            hash_page(RESOLVER, client=http)

    assert responding_url(caught.value, RESOLVER) == PUBLISHER
    assert responding_url(caught.value, RESOLVER) != RESOLVER


def test_without_a_redirect_the_answering_url_is_the_one_we_asked_for() -> None:
    """The negative control for the test above. If `responding_url` returned the
    publisher for everything, that test would pass while the function was
    simply wrong."""
    with _chain(status=403) as http:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            hash_page(DIRECT, client=http)

    assert responding_url(caught.value, DIRECT) == DIRECT


def test_an_error_carrying_no_address_falls_back_to_what_we_asked() -> None:
    """A DNS failure never reached a server, so there IS no responding host.
    Inventing one would manufacture the same kind of false finding as guessing
    `gone` from an unrecognised error."""
    assert responding_url(RuntimeError("no address here"), RESOLVER) == RESOLVER


def test_a_request_property_that_raises_does_not_escape() -> None:
    """httpx exposes `request` as a PROPERTY that raises RuntimeError when unset.
    `getattr(exc, "request", None)` swallows only AttributeError, so the
    RuntimeError would escape from inside the handler for a routine dead link
    and turn it into a crash."""

    class _Hostile(Exception):
        @property
        def request(self):
            raise RuntimeError("request was never set")

    assert responding_url(_Hostile(), RESOLVER) == RESOLVER


def test_an_oversized_page_still_reports_where_it_came_from() -> None:
    """TooLarge is raised mid-stream, after the redirect resolved, so it is one
    of the few places that KNOWS the answering host."""
    with _chain(status=200, body=b"x" * (HASH_MAX_BYTES + 1)) as http:
        with pytest.raises(TooLarge) as caught:
            hash_page(RESOLVER, client=http)

    assert responding_url(caught.value, RESOLVER) == PUBLISHER


# --- what gets stored --------------------------------------------------------


def test_a_blocked_row_records_the_publisher_not_the_resolver(db: Path) -> None:
    """End to end: real redirect, real hash_page, real snapshot, real sqlite.
    This is the 177-row defect, reproduced and then asserted away."""
    insert_url(db, RESOLVER, "KEY1", SEEN)

    with _chain(status=403) as http:
        snapshot(db, zotero=_Stamper(), hasher=_hasher(http), clock=lambda: "NOW")

    row = row_for_url(db, RESOLVER)
    assert row["last_outcome"] == BLOCKED
    assert row["final_url"] == PUBLISHER


def test_a_hashed_row_records_which_url_was_actually_hashed(db: Path) -> None:
    """The success path matters as much as the failure one: a hash whose subject
    is unknown is a weaker record than one that names its page. Leaving this out
    is what made the exception-only version of this fix a tripwire rather than a
    mechanism removal."""
    insert_url(db, RESOLVER, "KEY1", SEEN)

    with _chain(status=200) as http:
        result = snapshot(
            db, zotero=_Stamper(), hasher=_hasher(http), clock=lambda: "NOW"
        )

    assert result.hashed == 1
    row = row_for_url(db, RESOLVER)
    assert row["last_outcome"] == OK
    assert row["final_url"] == PUBLISHER
    assert row["content_hash"] != ""


def test_a_row_fetched_without_a_redirect_records_itself(db: Path) -> None:
    """Not empty. '' is reserved for "never found out", and a row we DID follow
    to its own address knows more than that."""
    insert_url(db, DIRECT, "KEY1", SEEN)

    with _chain(status=200) as http:
        snapshot(db, zotero=_Stamper(), hasher=_hasher(http), clock=lambda: "NOW")

    assert row_for_url(db, DIRECT)["final_url"] == DIRECT


def test_a_row_never_attempted_stays_empty(db: Path) -> None:
    """The third state, and the reason '' cannot be read as "no redirect". The
    ~4,900 rows written before this column existed never had their address
    checked, and must not be made to claim they did."""
    insert_url(db, RESOLVER, "KEY1", SEEN)

    row = row_for_url(db, RESOLVER)
    assert row["final_url"] == ""
    assert row["last_outcome"] == ""


# --- the schema reaches a legacy index ---------------------------------------


def test_the_column_is_added_to_an_index_that_predates_it(tmp_db: Path) -> None:
    """`snapshot_pages` died on the live index with `no such column:
    last_outcome` because only 2 of 8 CLIs called `init_db`. Every fixture here
    calls it first, which is exactly what made that invisible -- so this one
    takes the column back OUT and then goes through the ordinary read path."""
    from zotero_capture import sqlite_cache

    init_db(tmp_db)
    insert_url(tmp_db, RESOLVER, "KEY1", SEEN)
    with sqlite3.connect(tmp_db) as raw:
        raw.execute("ALTER TABLE url_index DROP COLUMN final_url")
    sqlite_cache._SCHEMA_READY.discard(str(tmp_db))

    # A plain read, with no init_db in front of it.
    assert row_for_url(tmp_db, RESOLVER)["final_url"] == ""


# --- the report --------------------------------------------------------------


def test_the_report_names_who_refused_us() -> None:
    result = SnapshotResult(
        examined=2,
        by_outcome={BLOCKED: 2},
        refused_by={"publisher.invalid": 2},
    )

    report = "\n".join(format_snapshot_report(result, dry_run=False))

    assert "publisher.invalid" in report


def test_the_report_says_nothing_about_hosts_when_nothing_was_refused() -> None:
    """Positive control against a header that prints unconditionally: a clean
    run must not grow an empty "who refused us" section."""
    result = SnapshotResult(examined=1, hashed=1, by_outcome={OK: 1})

    report = "\n".join(format_snapshot_report(result, dry_run=False))

    assert "who refused us" not in report


def test_a_too_large_page_is_attributed_to_its_host(db: Path) -> None:
    """`too_large` is our decision, not the host's, but the tally still has to
    name the host we spent the bandwidth on."""
    insert_url(db, RESOLVER, "KEY1", SEEN)

    with _chain(status=200, body=b"x" * (HASH_MAX_BYTES + 1)) as http:
        result = snapshot(
            db, zotero=_Stamper(), hasher=_hasher(http), clock=lambda: "NOW"
        )

    assert result.refused_by == {"publisher.invalid": 1}
    assert row_for_url(db, RESOLVER)["last_outcome"] == TOO_LARGE
    assert row_for_url(db, RESOLVER)["final_url"] == PUBLISHER
