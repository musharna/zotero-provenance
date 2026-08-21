"""backfill tests — one-off repair pass over items already in the collection."""

from __future__ import annotations

import json

import httpx

from zotero_capture.backfill import backfill
from zotero_capture.zotero_client import ZoteroClient


def _collection_client(items: list[dict], patches: list) -> ZoteroClient:
    """Serves a collection listing, per-item GETs, and records PATCHes."""
    by_key = {i["key"]: i for i in items}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/items/top"):
            start = int(req.url.params.get("start", 0))
            page = items[start : start + 100]
            return httpx.Response(200, json=page)
        key = path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(
                200, headers={"Last-Modified-Version": "1"}, json=by_key[key]
            )
        if req.method == "PATCH":
            patches.append((key, json.loads(req.content)))
            return httpx.Response(204)
        return httpx.Response(405)

    return ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(handler),
    )


def _item(key: str, title: str, url: str) -> dict:
    return {"key": key, "version": 1, "data": {"key": key, "title": title, "url": url}}


def test_backfill_repairs_only_the_unresolved_items():
    items = [
        _item("A", "https://a.test/x", "https://a.test/x"),  # unresolved
        _item("B", "A Real Title", "https://b.test/y"),  # already fine
    ]
    patches: list = []
    client = _collection_client(items, patches)
    result = backfill(client, lambda url: "Recovered", sleep_s=0)

    assert [k for k, _ in patches] == ["A"]
    assert patches[0][1]["title"] == "Recovered"
    assert result.fixed == 1
    assert result.examined == 1


def test_backfill_dry_run_patches_nothing():
    items = [_item("A", "https://a.test/x", "https://a.test/x")]
    patches: list = []
    client = _collection_client(items, patches)
    result = backfill(client, lambda url: "Recovered", dry_run=True, sleep_s=0)

    assert patches == [], "dry run must not write"
    assert result.would_fix == 1


def test_backfill_counts_items_that_stay_unresolved():
    items = [_item("A", "https://a.test/x", "https://a.test/x")]
    patches: list = []
    client = _collection_client(items, patches)
    # Fetcher fails: returns the URL back.
    result = backfill(client, lambda url: url, sleep_s=0)

    assert patches == []
    assert result.fixed == 0
    assert result.still_unresolved == 1


def test_backfill_respects_a_limit():
    items = [
        _item(f"K{i}", f"https://a.test/{i}", f"https://a.test/{i}") for i in range(5)
    ]
    patches: list = []
    client = _collection_client(items, patches)
    result = backfill(client, lambda url: "Recovered", limit=2, sleep_s=0)

    assert len(patches) == 2
    assert result.examined == 2
