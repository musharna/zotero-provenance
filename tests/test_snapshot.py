"""What the page said when it was read, so a change becomes detectable.

A captured item is a URL and a title. If the page is edited, paywalled or taken
down, nothing in the library shows what was actually consulted — which is the
one job a provenance record has. A hash does not preserve the content; it turns
"this citation might have said anything" into "this citation no longer says what
it said".

Two properties carry the whole feature and each is pinned here:

  * the hash covers the COMPLETE document or it is not recorded. Hashing a
    prefix would compare equal for two documents differing after the cap —
    a false negative in exactly the case the hash exists to catch, a long page
    quietly edited near the end.
  * a verify pass never rewrites a stored hash. The stored hash IS the evidence
    of what was consulted; replacing it with what the page says today would
    destroy the finding at the moment it was made.

`extra` is where the hash goes because the live API says so: `webpage` has no
`archive` or `archiveLocation` field. That was checked against
`/itemTypeFields?itemType=webpage` rather than assumed — the obvious guess would
have been silently dropped by Zotero.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from zotero_capture.snapshot import (
    HASH_MAX_BYTES,
    TooLarge,
    hash_page,
    snapshot,
    verify,
)
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    rows_needing_hash,
    rows_with_hash,
    set_content_hash,
)
from zotero_capture.zotero_client import ZoteroClient

URL = "https://fixturehost.org/a"
BODY = b"<html><title>T</title>the body</html>"
DIGEST = hashlib.sha256(BODY).hexdigest()


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


def _client(body: bytes, status: int = 200) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


class _Stamper:
    """A Zotero stand-in that records what it was asked to stamp."""

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.stamped: list[tuple[str, str]] = []

    def record_content_hash(self, item_key, digest, *, expect_url=None):
        self.stamped.append((item_key, digest))
        return self.accepts


# --- hashing ----------------------------------------------------------------


def test_the_hash_is_of_the_whole_body() -> None:
    with _client(BODY) as http:
        assert hash_page(URL, client=http) == DIGEST


def test_a_document_over_the_cap_is_refused_rather_than_truncated() -> None:
    """The property. A prefix hash would compare equal for two long documents
    that differ only after the cap."""
    with _client(b"x" * (HASH_MAX_BYTES + 1)) as http:
        with pytest.raises(TooLarge):
            hash_page(URL, client=http)


def test_a_document_at_the_cap_is_still_hashed() -> None:
    """Positive control for the refusal above: the boundary is a cap, not a
    blanket refusal to hash anything large."""
    body = b"x" * HASH_MAX_BYTES
    with _client(body) as http:
        assert hash_page(URL, client=http) == hashlib.sha256(body).hexdigest()


def test_an_http_error_raises() -> None:
    with _client(b"nope", status=404) as http:
        with pytest.raises(httpx.HTTPStatusError):
            hash_page(URL, client=http)


# --- the snapshot pass ------------------------------------------------------


def test_a_hashed_row_is_recorded_and_stamped(db: Path) -> None:
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))
    zotero = _Stamper()

    result = snapshot(db, zotero=zotero, hasher=lambda url: DIGEST, now="NOW")

    assert result.hashed_urls == [URL]
    assert zotero.stamped == [("KEY1", DIGEST)]
    assert rows_with_hash(db)[0]["content_hash"] == DIGEST


def test_the_index_is_not_written_when_the_stamp_is_refused(db: Path) -> None:
    """Order matters. Reversed, the index would claim a hash that appears
    nowhere in the library, and the next pass would skip the row for having
    one — a row permanently marked done that was never done.
    """
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))
    zotero = _Stamper(accepts=False)

    result = snapshot(db, zotero=zotero, hasher=lambda url: DIGEST, now="NOW")

    assert result.stamp_refused_urls == [URL]
    assert rows_with_hash(db) == []
    assert len(rows_needing_hash(db)) == 1, "the row must remain to be retried"


def test_an_unreachable_page_is_counted_not_hashed(db: Path) -> None:
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))

    def boom(url: str) -> str:
        raise httpx.ConnectError("dead link")

    result = snapshot(db, zotero=_Stamper(), hasher=boom, now="NOW")

    assert result.unreachable_urls == [URL]
    assert rows_with_hash(db) == []


def test_a_too_large_page_records_no_hash(db: Path) -> None:
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))

    def too_big(url: str) -> str:
        raise TooLarge(url)

    result = snapshot(db, zotero=_Stamper(), hasher=too_big, now="NOW")

    assert result.too_large_urls == [URL]
    assert rows_with_hash(db) == []


def test_an_in_flight_row_is_not_hashed(db: Path) -> None:
    """A reserved row has no item to stamp, and its capture may still be mid-POST."""
    from datetime import date

    from zotero_capture.sqlite_cache import reserve_url

    assert reserve_url(db, URL, date(2026, 5, 5), pending_key="PEND1234")
    assert rows_needing_hash(db) == [], "an in-flight row has no item to stamp"

    # Positive control: a COMPLETED row on the same index IS offered, so the
    # emptiness above is the reservation filter and not an empty query.
    insert_url(db, "https://fixturehost.org/done", "KEY9", date(2026, 5, 5))
    assert [r["url_canonical"] for r in rows_needing_hash(db)] == [
        "https://fixturehost.org/done"
    ]


def test_a_dry_run_fetches_nothing(db: Path) -> None:
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))

    def _unreachable(url: str) -> str:
        raise AssertionError("a dry run must not fetch")

    result = snapshot(db, zotero=None, hasher=_unreachable, now="NOW", dry_run=True)

    assert result.would_hash == 1
    assert rows_with_hash(db) == []


# --- verify -----------------------------------------------------------------


def test_a_changed_page_is_reported(db: Path) -> None:
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))
    set_content_hash(db, URL, content_hash=DIGEST, hashed_at="THEN")

    result = verify(db, hasher=lambda url: "a-different-digest")

    assert result.changed_urls == [URL]


def test_an_unchanged_page_is_not_reported(db: Path) -> None:
    """Positive control: a verify that flagged everything would pass the test
    above while telling you nothing."""
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))
    set_content_hash(db, URL, content_hash=DIGEST, hashed_at="THEN")

    result = verify(db, hasher=lambda url: DIGEST)

    assert result.changed_urls == [] and result.unchanged == 1


def test_verify_does_not_overwrite_the_stored_hash(db: Path) -> None:
    """The provenance property. The stored hash is the evidence of what was
    consulted; replacing it with today's content destroys the finding."""
    from datetime import date

    insert_url(db, URL, "KEY1", date(2026, 5, 5))
    set_content_hash(db, URL, content_hash=DIGEST, hashed_at="THEN")

    verify(db, hasher=lambda url: "a-different-digest")

    assert rows_with_hash(db)[0]["content_hash"] == DIGEST


# --- the Zotero write -------------------------------------------------------


def _stamping_client(item: dict, calls: list) -> ZoteroClient:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200, headers={"Last-Modified-Version": "7"}, json=item
            )
        calls.append(json.loads(req.content))
        return httpx.Response(204)

    return ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(handler),
    )


def test_stamping_preserves_other_extra_lines() -> None:
    """`extra` is a field people keep their own notes in."""
    item = {
        "key": "KEY1",
        "version": 7,
        "data": {"key": "KEY1", "url": URL, "extra": "PMID: 12345\nmine: keep me"},
    }
    calls: list = []

    assert _stamping_client(item, calls).record_content_hash("KEY1", DIGEST)

    extra = calls[0]["extra"]
    assert "PMID: 12345" in extra and "mine: keep me" in extra
    assert f"Content-SHA256: {DIGEST}" in extra


def test_stamping_replaces_a_previous_hash_rather_than_stacking() -> None:
    item = {
        "key": "KEY1",
        "version": 7,
        "data": {"key": "KEY1", "url": URL, "extra": "Content-SHA256: old"},
    }
    calls: list = []

    _stamping_client(item, calls).record_content_hash("KEY1", DIGEST)

    assert calls[0]["extra"].count("Content-SHA256:") == 1
    assert "old" not in calls[0]["extra"]


def test_stamping_refuses_an_item_that_moved_underneath_us() -> None:
    """Same guard as trash_item and update_url: the row was selected on a
    snapshot, and an item that is now something else is not the item hashed."""
    item = {
        "key": "KEY1",
        "version": 7,
        "data": {"key": "KEY1", "url": "https://fixturehost.org/SOMETHING-ELSE"},
    }
    calls: list = []

    assert not _stamping_client(item, calls).record_content_hash(
        "KEY1", DIGEST, expect_url=URL
    )
    assert calls == [], "it wrote to an item it had not selected"


def test_stamping_still_writes_when_the_url_matches() -> None:
    """Positive control for the refusal above."""
    item = {"key": "KEY1", "version": 7, "data": {"key": "KEY1", "url": URL}}
    calls: list = []

    assert _stamping_client(item, calls).record_content_hash(
        "KEY1", DIGEST, expect_url=URL
    )
    assert len(calls) == 1


# --- the incidental find ----------------------------------------------------


def test_delete_item_reports_whether_the_item_was_there() -> None:
    """It was annotated `-> None` while returning False on both 404 paths, so
    success and already-gone were both falsy and indistinguishable. Nothing
    checked, which is why it was worth fixing before something did."""

    def present(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200, headers={"Last-Modified-Version": "7"}, json={"version": 7}
            )
        return httpx.Response(204)

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(present),
    )
    assert client.delete_item("KEY1") is True

    gone = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(lambda req: httpx.Response(404)),
    )
    assert gone.delete_item("GHOST") is False
