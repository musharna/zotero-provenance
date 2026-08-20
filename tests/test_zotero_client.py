"""zotero_client tests — POST item, PATCH tags, query by tag. Mocked + one live."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from zotero_capture.zotero_client import ZoteroClient, ZoteroError


def test_post_webpage_item_returns_key():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "successful": {"0": {"key": "ITEM123", "version": 1}},
                "failed": {},
                "success": {"0": "ITEM123"},
                "unchanged": {},
            },
        )

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL123",
        transport=httpx.MockTransport(handler),
    )
    key = client.post_webpage_item(
        url_canonical="https://example.com/foo",
        title="Foo",
        access_date="2026-05-05",
        tags=["context:general", "seen:2026-05-05", "domain:example.com"],
    )
    assert key == "ITEM123"
    assert "items" in captured["url"]
    assert captured["body"][0]["url"] == "https://example.com/foo"
    assert captured["body"][0]["collections"] == ["COLL123"]
    assert {"tag": "context:general"} in captured["body"][0]["tags"]


def test_query_by_tag_returns_items():
    sample = [
        {
            "key": "A",
            "data": {
                "tags": [{"tag": "context:lit-review"}, {"tag": "seen:2026-05-05"}]
            },
        },
        {
            "key": "B",
            "data": {
                "tags": [{"tag": "context:lit-review"}, {"tag": "seen:2026-04-29"}]
            },
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sample)

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL123",
        transport=httpx.MockTransport(handler),
    )
    items = client.query_by_tag("context:lit-review")
    assert {i["key"] for i in items} == {"A", "B"}


def test_patch_add_tag_dedups():
    """PATCH must not duplicate an existing tag."""
    state = {
        "version": 1,
        "tags": [{"tag": "context:general"}, {"tag": "seen:2026-05-04"}],
    }
    requests_made: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests_made.append((req.method, str(req.url)))
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": str(state["version"])},
                json={
                    "key": "ITEM1",
                    "version": state["version"],
                    "data": {"version": state["version"], "tags": list(state["tags"])},
                },
            )
        if req.method == "PATCH":
            body = json.loads(req.content)
            state["tags"] = body["tags"]
            state["version"] += 1
            return httpx.Response(204)
        return httpx.Response(405)

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL123",
        transport=httpx.MockTransport(handler),
    )
    client.add_tags("ITEM1", ["context:general"])
    methods = [m for m, _ in requests_made]
    assert "PATCH" not in methods, "should not PATCH when no new tags"

    requests_made.clear()
    client.add_tags("ITEM1", ["seen:2026-05-05"])
    methods = [m for m, _ in requests_made]
    assert methods == ["GET", "PATCH"]
    assert {t["tag"] for t in state["tags"]} == {
        "context:general",
        "seen:2026-05-04",
        "seen:2026-05-05",
    }


def test_add_tags_412_raises_zotero_error():
    """412 conflict (concurrent writer bumped version) must raise ZoteroError, not httpx.HTTPStatusError."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "5"},
                json={"key": "ITEM1", "version": 5, "data": {"tags": []}},
            )
        return httpx.Response(412, text="version conflict")

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ZoteroError, match="412"):
        client.add_tags("ITEM1", ["seen:2026-05-05"])


def test_delete_item_tolerates_404_from_get():
    """If item already deleted, GET returns 404 → short-circuit, no DELETE issued."""
    requests_made: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests_made.append(req.method)
        return httpx.Response(404)

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL",
        transport=httpx.MockTransport(handler),
    )
    client.delete_item("GHOST")  # must not raise
    assert requests_made == ["GET"], "DELETE must not be sent when GET returns 404"


def test_delete_item_tolerates_404_from_delete():
    """If GET succeeds but DELETE returns 404 (race with another deleter), still no raise."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "3"},
                json={"key": "RACE", "version": 3, "data": {}},
            )
        return httpx.Response(404)  # DELETE 404

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL",
        transport=httpx.MockTransport(handler),
    )
    client.delete_item("RACE")  # must not raise


def test_post_webpage_item_raises_on_failed_entries():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"successful": {}, "failed": {"0": {"code": 400, "message": "bad"}}},
        )

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ZoteroError, match="failed entries"):
        client.post_webpage_item(
            url_canonical="https://example.com/x",
            title="X",
            access_date="2026-05-05",
            tags=[],
        )


def test_post_webpage_item_raises_on_empty_successful():
    """Defensive guard: empty successful map must surface as ZoteroError, not StopIteration."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"successful": {}, "failed": {}})

    client = ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="group",
        web_sources_collection_key="COLL",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ZoteroError, match="no successful entries"):
        client.post_webpage_item(
            url_canonical="https://example.com/x",
            title="X",
            access_date="2026-05-05",
            tags=[],
        )


@pytest.mark.live
def test_live_post_then_query_then_delete(live_zotero_creds: dict[str, str]):
    """Real-execution check at the Zotero API boundary."""
    coll_key = os.environ.get("ZOTERO_WEBSOURCES_COLLECTION_KEY_TEST")
    if not coll_key:
        pytest.skip(
            "ZOTERO_WEBSOURCES_COLLECTION_KEY_TEST not set (use the test sub-collection from Task 2)"
        )
    client = ZoteroClient(
        api_key=live_zotero_creds["api_key"],
        library_id=live_zotero_creds["library_id"],
        library_type=live_zotero_creds["library_type"],
        web_sources_collection_key=coll_key,
    )
    key = client.post_webpage_item(
        url_canonical="https://example.com/test-client-post",
        title="Test client post",
        access_date="2026-05-05",
        tags=["context:test-poc", "seen:2026-05-05"],
    )
    items = client.query_by_tag("context:test-poc")
    assert any(i["key"] == key for i in items)
    client.delete_item(key)
