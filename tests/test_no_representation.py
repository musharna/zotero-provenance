"""A status that carries no representation is not a document.

`raise_for_status` asks "did the request fail?". It never asks "did we receive
a document?", and 202 is a SUCCESS status -- so a response with no body at all
passed every gate this module had, was hashed as `sha256("")`, and became the
provenance record for the citation. `verify` then re-read the same 202, found
the digest matched, and reported `unchanged`: the tool asserting a source is
intact when it had never once received the source.

Measured live 2026-09-05, all 26 index rows whose stored hash is of zero bytes,
probed through the production client with a control that returned 9,083 bytes:

    24 x HTTP 202, Content-Length: 0, text/html
         server: CloudFront (18) / awselb/2.0 (6)
     1 x HTTP 200, Content-Length: 0, text/xml   <- a REAL empty response
     1 x HTTP 200, 168,811 bytes                 <- the page came back

Who answered: ieeexplore (7), figshare (4), sketchfab (3), morningstar (3),
dataverse.harvard.edu (2), semanticscholar (2), degruyterbrill, cgtrader, jove.
Six of the nine `doi.org` rows resolve onto ieeexplore, which is only visible
because the responding address is recorded -- all 26 rows predate 0.38.0 and
carry an empty `final_url`.

THE FIX IS ON THE STATUS, NOT ON THE EMPTINESS, and the third test below is why:
`biodiversitylibrary.org/api3` really does answer 200 with zero bytes. Refusing
to hash an empty body would throw away a true fact to catch a false one, and
would still hash a 202 that shipped a challenge body. 202/204/205 carry no
representation BY DEFINITION (RFC 9110); that is the property, and it holds
whatever the body length turns out to be.

NOT folded into BLOCKED. `NotTheResource` already routes a challenge page there
and "refused; the page may be perfectly fine" is exactly right for a bot wall --
but a 204 is not a refusal, and calling it one would assert a fact about the
host that never happened. That is the `unreachable`-means-gone-AND-blocked
defect, which has cost this project a release twice.

BLAST RADIUS IS A LOWER BOUND. Response status is not stored, so a 202 that
carried a challenge body is indistinguishable in the index from a real document.
26 is what this signature can see, not what happened.
"""

from __future__ import annotations

import hashlib
from datetime import date

import httpx
import pytest

from zotero_capture.snapshot import (
    BLOCKED,
    NO_CONTENT,
    NoRepresentation,
    classify_failure,
    hash_page,
    responding_url,
    verify,
)
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    row_for_url,
    set_content_hash,
)

PAGE = "https://publisher.invalid/articles/1"
SEEN = date(2026, 9, 5)


def _answering(status: int, body: bytes = b"", *, location: str | None = None) -> httpx.Client:
    """A transport that answers with one status, and nothing else.

    `location` exists so the redirect case is driven end to end: the recorded
    address must be the host that ANSWERED, not the one we asked. Nine rows in
    the live index asked doi.org and were refused by ieeexplore.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if location and str(req.url) == PAGE:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(
            status, content=body, headers={"content-type": "text/html; charset=utf-8"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# --- the property -------------------------------------------------------------


@pytest.mark.parametrize("status", [202, 204, 205])
def test_a_status_carrying_no_representation_is_refused_rather_than_hashed(status) -> None:
    """THE property. A success status with no representation is not the page."""
    with _answering(status) as http:
        with pytest.raises(NoRepresentation):
            hash_page(PAGE, client=http)


def test_a_202_that_ships_a_body_is_still_refused() -> None:
    """The status is the property, not the length.

    A challenge page served with 202 AND a body is the case an emptiness test
    would miss entirely, which is why the emptiness test is not the fix.
    """
    with _answering(202, b"<html><body>checking your browser</body></html>") as http:
        with pytest.raises(NoRepresentation):
            hash_page(PAGE, client=http)


def test_a_200_that_really_is_empty_is_still_hashed() -> None:
    """THE POSITIVE CONTROL, and a real row: biodiversitylibrary.org/api3.

    Without this, a fix that refused every empty body would pass the tests above
    while destroying a true record. An empty document is a fact about the source
    and this tool exists to keep facts about sources.
    """
    with _answering(200) as http:
        read = hash_page(PAGE, client=http)
    assert read.digest == hashlib.sha256(b"").hexdigest()
    assert read.covers_bytes == 0


# --- what it is called --------------------------------------------------------


def test_no_representation_is_not_reported_as_a_refusal() -> None:
    """`blocked` says the host refused us. A 204 refused nothing."""
    assert classify_failure(NoRepresentation(status=204, final_url=PAGE)) == NO_CONTENT
    assert NO_CONTENT != BLOCKED


def test_the_refusal_names_the_host_that_ANSWERED() -> None:
    """Six live rows asked doi.org and were refused by ieeexplore.

    `responding_url` reads `final_url` first, so the attribute has to carry that
    exact name -- the 0.38.0 lesson, where 177 rows blamed doi.org for a refusal
    one hop later.
    """
    answered = "https://ieeexplore.invalid/document/1"
    with _answering(202, location=answered) as http:
        with pytest.raises(NoRepresentation) as caught:
            hash_page(PAGE, client=http)
    assert responding_url(caught.value, PAGE) == answered


def test_the_refusal_carries_the_status_it_saw() -> None:
    """A fault that cannot say what it saw cannot be argued with later."""
    with _answering(202) as http:
        with pytest.raises(NoRepresentation) as caught:
            hash_page(PAGE, client=http)
    assert caught.value.status == 202


# --- the defect itself --------------------------------------------------------


def test_a_row_re_read_as_202_is_no_longer_reported_unchanged(tmp_path) -> None:
    """THE DEFECT, end to end.

    25 live rows stored `sha256("")`, were re-read against the same 202, and
    were reported `unchanged` -- a digest match between two refusals.
    """
    db = tmp_path / "url_index.db"
    init_db(db)
    insert_url(db, PAGE, "K1", SEEN)
    set_content_hash(
        db, PAGE,
        content_hash=hashlib.sha256(b"").hexdigest(),
        hashed_at="2026-08-29T00:00:00", covers_bytes=0, complete=True,
        sketch="", sketch_algo="",
    )

    def hasher(url: str, max_bytes: int) -> object:
        with _answering(202) as http:
            return hash_page(url, client=http, max_bytes=max_bytes)

    result = verify(db, hasher=hasher, clock=lambda: "2026-09-05T00:00:00")
    assert result.unchanged == 0, "a refusal matched a refusal and was called agreement"
    assert result.unreachable == 1
    assert row_for_url(db, PAGE)["verify_outcome"] == NO_CONTENT


def test_the_stored_hash_survives_a_no_representation_re_read(tmp_path) -> None:
    """The hash is the record of what was consulted; a refusal does not erase it.

    Even a hash that should never have been written is evidence of what this
    tool did, and destroying it is a separate decision from declining to trust
    it.
    """
    db = tmp_path / "url_index.db"
    init_db(db)
    insert_url(db, PAGE, "K1", SEEN)
    original = hashlib.sha256(b"").hexdigest()
    set_content_hash(
        db, PAGE, content_hash=original, hashed_at="2026-08-29T00:00:00",
        covers_bytes=0, complete=True, sketch="", sketch_algo="",
    )

    def hasher(url: str, max_bytes: int) -> object:
        with _answering(202) as http:
            return hash_page(url, client=http, max_bytes=max_bytes)

    verify(db, hasher=hasher, clock=lambda: "2026-09-05T00:00:00")
    assert row_for_url(db, PAGE)["content_hash"] == original
