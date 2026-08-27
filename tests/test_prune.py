"""prune tests — remove items the exclusion rules say should never have been captured.

Trash, not delete: Zotero's DELETE is permanent, while PATCH deleted:1 lands the
item in the trash where a human can still get it back. A cleanup pass over a
research provenance record should be undoable.
"""

from __future__ import annotations

import json

import httpx

from zotero_capture.prune import prune
from zotero_capture.zotero_client import ZoteroClient


def _client(items: list[dict], calls: list) -> ZoteroClient:
    by_key = {i["key"]: i for i in items}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/items/top"):
            start = int(req.url.params.get("start", 0))
            return httpx.Response(200, json=items[start : start + 100])
        key = path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(
                200, headers={"Last-Modified-Version": "7"}, json=by_key[key]
            )
        if req.method == "PATCH":
            calls.append((key, json.loads(req.content), dict(req.headers)))
            return httpx.Response(204)
        if req.method == "DELETE":
            calls.append((key, "PERMANENT-DELETE", None))
            return httpx.Response(204)
        return httpx.Response(405)

    return ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(handler),
    )


def _item(key: str, url: str) -> dict:
    return {"key": key, "version": 7, "data": {"key": key, "title": url, "url": url}}


def test_prune_trashes_excluded_items_and_leaves_real_sources():
    """Positive control lives in the same test: a real source must survive."""
    items = [
        _item("JUNK1", "https://evil.example.com/x"),
        _item("JUNK2", "https://fonts.googleapis.com/css2?family=Inter"),
        _item("REAL1", "https://doi.org/10.1016/s0092-8674(00)80876-3"),
        _item("REAL2", "https://en.wikipedia.org/wiki/Aestivation_(botany)"),
    ]
    calls: list = []
    result = prune(_client(items, calls), sleep_s=0)

    assert sorted(k for k, _, _ in calls) == ["JUNK1", "JUNK2"]
    assert result.trashed == 2
    assert result.examined == 4


def test_prune_uses_the_trash_not_a_permanent_delete():
    """Regression guard: the client's delete_item is a permanent DELETE."""
    calls: list = []
    result = prune(_client([_item("JUNK1", "http://test")], calls), sleep_s=0)

    assert len(calls) == 1
    key, body, headers = calls[0]
    assert body != "PERMANENT-DELETE", "must not use DELETE; the trash is recoverable"
    assert body["deleted"] == 1
    assert headers["if-unmodified-since-version"] == "7"
    assert result.trashed == 1


def test_prune_dry_run_writes_nothing():
    items = [_item("JUNK1", "https://evil.example.com/x")]
    calls: list = []
    result = prune(_client(items, calls), dry_run=True, sleep_s=0)

    assert calls == [], "dry run must not write"
    assert result.would_trash == 1
    assert result.trashed == 0


def test_prune_reports_what_it_would_remove():
    """The caller needs the list, so a human can eyeball it before writing."""
    items = [
        _item("JUNK1", "https://evil.example.com/x"),
        _item("REAL1", "https://arxiv.org/abs/2401.00001"),
    ]
    result = prune(_client(items, []), dry_run=True, sleep_s=0)

    assert result.selected == ["https://evil.example.com/x"]


def test_prune_skips_items_with_no_url():
    items = [{"key": "NOURL", "version": 7, "data": {"key": "NOURL", "title": "x"}}]
    calls: list = []
    result = prune(_client(items, calls), sleep_s=0)

    assert calls == []
    assert result.trashed == 0


def _live_client(items: list[dict], calls: list) -> ZoteroClient:
    """A mock that behaves like Zotero: trashing REMOVES the item from listings.

    The mock above keeps every patched item in the list, so it cannot fail on a
    loop that mutates while paginating — which is exactly the bug this exercises.
    """
    live = list(items)
    by_key = {i["key"]: i for i in items}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/items/top"):
            start = int(req.url.params.get("start", 0))
            limit = int(req.url.params.get("limit", 100))
            return httpx.Response(200, json=live[start : start + limit])
        key = path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(
                200, headers={"Last-Modified-Version": "7"}, json=by_key[key]
            )
        if req.method == "PATCH":
            calls.append(key)
            body = json.loads(req.content)
            if body.get("deleted") == 1:
                live[:] = [i for i in live if i["key"] != key]
            return httpx.Response(204)
        return httpx.Response(405)

    return ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(handler),
    )


def test_prune_does_not_skip_items_when_trashing_shifts_the_pages():
    """Trashing during an offset walk used to step over its own gap.

    Zotero excludes trashed items from normal listings, so removing entries from
    page 1 shifts everything left; asking for start=100 next then skips as many
    entries as were removed. Sized past one page so the shift actually bites.
    """
    items = []
    for n in range(250):
        junk = n % 3 == 0
        url = (
            f"https://fonts.gstatic.com/f{n}.woff2"
            if junk
            else f"https://arxiv.org/abs/2401.{n:05d}"
        )
        items.append(_item(f"K{n:04d}", url))
    expected_junk = {i["key"] for i in items if "gstatic" in i["data"]["url"]}

    calls: list = []
    result = prune(_live_client(items, calls), sleep_s=0)

    assert set(calls) == expected_junk, "every excluded item must be trashed exactly once"
    assert result.trashed == len(expected_junk)
    assert result.examined == 250, "and every item must have been examined"
