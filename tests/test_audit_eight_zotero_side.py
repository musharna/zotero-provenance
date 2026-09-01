"""What happens when a human edits the library underneath the index.

Capture treats Zotero as append-only storage it alone writes to. It is a shared
library a person also uses, and every one of these is an ordinary thing to do:
trash an item, fix a bad title by hand, move something. Each left the index in a
state it could never leave.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zotero_capture.zotero_client import (
    UNRESOLVED_TITLE_TAG,
    ItemGone,
    ZoteroClient,
    ZoteroError,
)


def _client(handler) -> ZoteroClient:
    return ZoteroClient(
        api_key="k",
        library_id="1",
        library_type="user",
        web_sources_collection_key="COLL",
        transport=httpx.MockTransport(handler),
    )


def test_tagging_a_deleted_item_reports_it_as_gone():
    """A trashed item must be distinguishable from a broken API.

    Before: GET 404 raised a generic ZoteroError, capture logged it and left the
    row untouched, so every future citation of that URL repeated the same 404
    forever and the source was never recorded again.
    """
    client = _client(lambda req: httpx.Response(404, json={}))
    with pytest.raises(ItemGone):
        client.add_tags("GONE1234", ["seen:2026-05-05"])


def test_tagging_a_trashed_item_reports_it_as_gone():
    """Zotero's trash is `deleted: 1`, not a 404 — the same situation."""
    client = _client(
        lambda req: httpx.Response(
            200, json={"data": {"key": "T", "deleted": 1, "tags": []}, "version": 3}
        )
    )
    with pytest.raises(ItemGone):
        client.add_tags("TRASHED1", ["seen:2026-05-05"])


def test_a_real_api_failure_is_still_a_plain_error():
    """Positive control: ItemGone must mean gone, not 'anything went wrong'."""
    client = _client(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(ZoteroError) as e:
        client.add_tags("ITEM1234", ["seen:2026-05-05"])
    assert not isinstance(e.value, ItemGone)


def test_a_title_a_human_fixed_is_not_overwritten():
    """A stale tag must not authorise replacing real metadata.

    title_is_unresolved trusts the tag over the title it can see. So a user who
    fixed a bad title by hand, leaving the tag, had their title silently
    replaced by whatever the fetcher returned next. The tag is a claim about the
    title; the title is the evidence, and the evidence wins.
    """
    patched: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "key": "ITEM1234",
                        "title": "A Title The User Typed",
                        "url": "https://example.org/p",
                        "tags": [{"tag": UNRESOLVED_TITLE_TAG}],
                    },
                    "version": 7,
                },
                headers={"Last-Modified-Version": "7"},
            )
        patched["body"] = json.loads(req.content)
        return httpx.Response(204)

    client = _client(handler)
    client.add_tags(
        "ITEM1234", ["seen:2026-05-05"], title_resolver=lambda _url: "Fetched Title"
    )
    assert "title" not in patched["body"], (
        f"overwrote a human's title: {patched['body'].get('title')!r}"
    )
    tags = {t["tag"] for t in patched["body"]["tags"]}
    assert UNRESOLVED_TITLE_TAG not in tags, "the stale tag should be dropped"


def test_a_genuinely_unresolved_title_is_still_filled_in():
    """Positive control: the re-enrichment this plugin exists for must work."""
    patched: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "key": "ITEM1234",
                        "title": "https://example.org/p",
                        "url": "https://example.org/p",
                        "tags": [{"tag": UNRESOLVED_TITLE_TAG}],
                    },
                    "version": 7,
                },
                headers={"Last-Modified-Version": "7"},
            )
        patched["body"] = json.loads(req.content)
        return httpx.Response(204)

    client = _client(handler)
    client.add_tags(
        "ITEM1234", ["seen:2026-05-05"], title_resolver=lambda _url: "Real Title"
    )
    assert patched["body"]["title"] == "Real Title"


def test_update_url_reports_whether_it_actually_updated():
    """A 404 is not a successful rewrite.

    update_url returned None either way, so repair updated its SQLite row and
    counted a rewrite for an item that does not exist — the index then claims a
    corrected URL backed by nothing.
    """
    gone = _client(lambda req: httpx.Response(404, json={}))
    assert gone._patch_item_url("GONE1234", "https://example.org/fixed") is False

    def ok(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={"data": {"key": "I", "title": "T", "url": "https://a/"}, "version": 2},
                headers={"Last-Modified-Version": "2"},
            )
        return httpx.Response(204)

    assert _client(ok)._patch_item_url("ITEM1234", "https://example.org/fixed") is True


def test_capture_drops_a_row_whose_item_a_person_deleted(tmp_path):
    """The index must not keep pointing at an item that is gone.

    Left in place, the row made every future citation of that URL repeat the
    same 404 forever: the source silently stopped being recorded and nothing
    said so.
    """
    from datetime import date
    from unittest.mock import MagicMock

    from zotero_capture.capture import capture_message
    from zotero_capture.sqlite_cache import init_db, insert_url, lookup_url

    db = tmp_path / "url_index.db"
    init_db(db)
    url = "https://fixturehost.org/deleted-by-hand"
    insert_url(db, url, "GONE1234", date(2026, 5, 5))

    z = MagicMock()
    z.add_tags.side_effect = ItemGone("item GONE1234 is in the trash")
    result = capture_message(
        message=f"See {url} here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=z,
        title_fetcher=lambda u: "T",
    )

    assert lookup_url(db, url) is None, "the stale row survived"
    assert [e.code for e in result.errors] == ["item_gone"]


def test_capture_keeps_the_row_when_zotero_is_merely_broken(tmp_path):
    """Positive control: a 500 is transient and must NOT destroy the row."""
    from datetime import date
    from unittest.mock import MagicMock

    from zotero_capture.capture import capture_message
    from zotero_capture.sqlite_cache import init_db, insert_url, lookup_url

    db = tmp_path / "url_index.db"
    init_db(db)
    url = "https://fixturehost.org/transient"
    insert_url(db, url, "ITEM1234", date(2026, 5, 5))

    z = MagicMock()
    z.add_tags.side_effect = ZoteroError("500 boom")
    capture_message(
        message=f"See {url} here.",
        project_slug="home",
        context=None,
        today=date(2026, 5, 5),
        db_path=db,
        zotero=z,
        title_fetcher=lambda u: "T",
    )
    assert lookup_url(db, url) is not None, "a transient error destroyed the row"
