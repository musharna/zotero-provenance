"""A title resolved once must survive the retry that follows a lost race.

add_tags does a read-modify-write against an item version, so a 412 means
another session wrote in between and the whole read-modify-write is retried.
Title re-enrichment rode along inside that: the resolver was called again on
every attempt.

A title fetch is a live HTTP request, so it can succeed on the first attempt and
fail on the second. When it did, the successful title was thrown away, the
retried PATCH went out with tags only, `title:unresolved` stayed applied, and
add_tags returned True — a silent loss reported as success. It also meant one
contended item could burn the whole per-run re-enrichment budget.

Reported by an external audit of v0.10.0 (2026-08-22).
"""

from __future__ import annotations

import json

import httpx

from zotero_capture.zotero_client import UNRESOLVED_TITLE_TAG, ZoteroClient

URL = "https://fixturehost.org/a"


def _client(handler) -> ZoteroClient:
    return ZoteroClient(
        api_key="k",
        library_id="1",
        web_sources_collection_key="C",
        transport=httpx.MockTransport(handler),
    )


def _unresolved_item(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "version": 5,
            "data": {"title": URL, "url": URL, "tags": [{"tag": UNRESOLVED_TITLE_TAG}]},
        },
        headers={"Last-Modified-Version": "5"},
    )


def _make_handler(patch_statuses: list[int], patches: list[dict]):
    remaining = list(patch_statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _unresolved_item(request)
        patches.append(json.loads(request.content))
        return httpx.Response(remaining.pop(0))

    return handler


def _flaky_resolver(results: list[str]):
    remaining = list(results)

    def resolve() -> str:
        return remaining.pop(0)

    return resolve


def test_a_title_resolved_before_a_412_is_reapplied_after_it():
    """The resolver succeeds, loses the race, then fails. The title must survive."""
    patches: list[dict] = []
    client = _client(_make_handler([412, 204], patches))
    ok = client.add_tags(
        "KEY",
        ["context:mine"],
        title_resolver=_flaky_resolver(["Real Title", URL]),
    )
    assert ok
    final = patches[-1]
    assert final.get("title") == "Real Title", "the resolved title was dropped"
    assert not any(t["tag"] == UNRESOLVED_TITLE_TAG for t in final["tags"])


def test_the_resolver_is_not_called_again_once_it_has_succeeded():
    """Re-asking costs a live fetch out of a budget shared with every other URL."""
    patches: list[dict] = []
    client = _client(_make_handler([412, 412, 204], patches))
    calls = {"n": 0}

    def resolve() -> str:
        calls["n"] += 1
        return "Real Title"

    client.add_tags("KEY", ["context:mine"], title_resolver=resolve)
    assert calls["n"] == 1


def test_a_resolver_that_never_succeeds_still_leaves_the_item_marked():
    """Negative control: nothing is invented when resolution genuinely fails."""
    patches: list[dict] = []
    client = _client(_make_handler([204], patches))
    client.add_tags("KEY", ["context:mine"], title_resolver=_flaky_resolver([URL]))
    final = patches[-1]
    assert "title" not in final
    assert any(t["tag"] == UNRESOLVED_TITLE_TAG for t in final["tags"])
