"""A destructive call must re-check the evidence it was selected on.

Every one of these tools decides WHAT to destroy from a snapshot, then destroys
it later by key. Optimistic versioning protects the write from a concurrent
edit, but not from an edit that happened BEFORE the final GET: the client
faithfully fetches the item's new version and patches it. So the pass reports
trashing a font asset while it actually trashed a paper.

The fix is that the destructive calls take the URL the decision was made on, and
refuse when the item no longer matches it.
"""

from __future__ import annotations

import httpx

from zotero_capture.zotero_client import ZoteroClient

EXCLUDED = "https://fonts.gstatic.com/s/font.woff2"
LEGITIMATE = "https://doi.org/10.1000/real-paper"


def _client(handler) -> ZoteroClient:
    return ZoteroClient(
        api_key="k",
        library_id="1",
        library_type="user",
        web_sources_collection_key="COLL",
        transport=httpx.MockTransport(handler),
    )


def _serving(current_url: str, seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "9"},
                json={"key": "ITEM1234", "version": 9,
                      "data": {"key": "ITEM1234", "version": 9,
                               "url": current_url, "title": "t"}},
            )
        return httpx.Response(204)

    return handler


def test_trash_refuses_an_item_whose_url_no_longer_matches() -> None:
    """Selected as a font asset; by the time we trash it, it is a paper."""
    seen: list[str] = []
    client = _client(_serving(LEGITIMATE, seen))

    trashed = client.trash_item("ITEM1234", expect_url=EXCLUDED)

    assert trashed is False, "an item was trashed on evidence that no longer held"
    assert not any(s.startswith("PATCH") for s in seen), f"it was patched anyway: {seen}"


def test_trash_still_works_when_the_url_still_matches() -> None:
    """Positive control: a refusal that refuses everything is not a fix."""
    seen: list[str] = []
    client = _client(_serving(EXCLUDED, seen))

    assert client.trash_item("ITEM1234", expect_url=EXCLUDED) is True
    assert any(s.startswith("PATCH") for s in seen), seen


def test_trash_without_an_expectation_is_unchanged() -> None:
    """Callers that have no snapshot to check against must keep working."""
    seen: list[str] = []
    client = _client(_serving(LEGITIMATE, seen))

    assert client.trash_item("ITEM1234") is True


def test_update_url_refuses_an_item_that_moved_under_it() -> None:
    """Repair rewrites A->A'. If the item is now B, the rewrite is not repair."""
    seen: list[str] = []
    client = _client(_serving(LEGITIMATE, seen))

    moved = client._patch_item_url("ITEM1234", "https://example.org/fixed",
                              expect_url=EXCLUDED)

    assert moved is False
    assert not any(s.startswith("PATCH") for s in seen), seen


def test_update_url_still_rewrites_the_item_it_selected() -> None:
    """Positive control."""
    seen: list[str] = []
    client = _client(_serving(EXCLUDED, seen))

    assert client._patch_item_url("ITEM1234", "https://example.org/fixed",
                             expect_url=EXCLUDED) is True
    assert any(s.startswith("PATCH") for s in seen), seen


# --- and the callers must actually PASS their evidence ----------------------


def test_prune_passes_the_snapshot_url_it_selected_on() -> None:
    """The walk completes before any write, so minutes pass in between."""
    from zotero_capture.prune import prune

    asked: list[tuple[str, str | None]] = []

    class _Zotero:
        def iter_collection_items(self):
            yield {"key": "ITEM1234", "data": {"url": EXCLUDED}}

        def trash_item(self, key, *, expect_url=None):
            asked.append((key, expect_url))
            return True

    result = prune(_Zotero(), sleep_s=0)

    assert result.trashed == 1, "positive control: the excluded item was trashed"
    assert asked == [("ITEM1234", EXCLUDED)], f"prune destroyed blind: {asked}"


def test_prune_counts_a_refusal_rather_than_reporting_a_trash() -> None:
    """A refused delete must not be reported as one."""
    from zotero_capture.prune import prune

    class _Refuses:
        def iter_collection_items(self):
            yield {"key": "ITEM1234", "data": {"url": EXCLUDED}}

        def trash_item(self, key, *, expect_url=None):
            return False

    result = prune(_Refuses(), sleep_s=0)

    assert result.trashed == 0 and result.skipped == 1, result


def test_retire_passes_the_url_it_planned_on() -> None:
    from zotero_capture.retire import RetireStep, apply_retire

    asked: list[tuple[str, str | None]] = []

    class _Zotero:
        def trash_item(self, key, *, expect_url=None):
            asked.append((key, expect_url))
            return False  # the item is no longer what was planned

    import sqlite3
    import tempfile
    from pathlib import Path as _P

    from zotero_capture.sqlite_cache import init_db

    db = _P(tempfile.mkdtemp()) / "idx.db"
    init_db(db)
    url = "https://files.rcsb.org/download/{ID}.pdb"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES (?, 'K1', '2026-01-01', '2026-01-01')",
            (url,),
        )

    def _connect(path):
        c = sqlite3.connect(path, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    counts = apply_retire(
        [RetireStep(url, "K1", "junk", "hard")],
        db_path=db, zotero=_Zotero(), connect=_connect,
    )

    assert asked == [("K1", url)], f"retire destroyed blind: {asked}"
    assert counts["skipped"] == 1 and counts["trashed"] == 0, counts
    with sqlite3.connect(db) as conn:
        left = [r[0] for r in conn.execute("SELECT url_canonical FROM url_index")]
    assert left == [url], "the row was dropped for an item that was not trashed"


# --- the same bug in the title path: a resolver closed over a stale URL -------


def test_the_title_resolver_is_asked_about_the_item_it_is_writing_to() -> None:
    """Backfill closed its resolver over the SNAPSHOT url; add_tags refetches.

    So it could fetch url A's title, find the item is now url B, and write A's
    title onto B -- then clear the unresolved marker and count it fixed. The
    version guard cannot see this: the current version is fetched after the URL
    changed.
    """
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "9"},
                json={"key": "ITEM1234", "version": 9,
                      "data": {"key": "ITEM1234", "version": 9,
                               "url": LEGITIMATE, "title": LEGITIMATE, "tags": []}},
            )
        return httpx.Response(204)

    def resolver(url: str) -> str:
        asked.append(url)
        return "Title of whatever it was asked about"

    _client(handler).add_tags("ITEM1234", [], title_resolver=resolver)

    assert asked == [LEGITIMATE], (
        f"the resolver was asked about the wrong item's url: {asked}"
    )
