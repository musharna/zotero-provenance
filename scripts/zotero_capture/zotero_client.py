"""Zotero source capture: hook-driven URL ingestion into a Zotero collection."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import USER_AGENT
from .sqlite_cache import new_zotero_key

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.zotero.org"

# Marks an item whose title could not be fetched, so the URL-as-fallback stays
# recognisable as a failure instead of being mistaken for real metadata.
UNRESOLVED_TITLE_TAG = "title:unresolved"


def api_base() -> str:
    """The Zotero API root. Overridable for tests and API-compatible servers."""
    return os.environ.get("ZOTERO_API_BASE", DEFAULT_API_BASE).rstrip("/")


class ZoteroError(Exception):
    """Surfaced for any unrecoverable Zotero API failure."""


class ZoteroClient:
    def __init__(
        self,
        *,
        api_key: str,
        library_id: str,
        library_type: str = "group",
        web_sources_collection_key: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
    ):
        self.library_id = library_id
        self.library_type = library_type
        self.collection_key = web_sources_collection_key
        self._client = httpx.Client(
            base_url=f"{api_base()}/{library_type}s/{library_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Zotero-API-Version": "3",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ZoteroClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def item_exists(self, item_key: str) -> bool:
        """Whether the library already holds this key.

        The question a lost POST response leaves behind. It is only answerable
        because the key was chosen before the request went out.
        """
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        return not (resp.json().get("data", {}).get("deleted"))

    def get_item_tags(self, item_key: str) -> list[str]:
        """Every tag on an item, or none if it is already gone.

        Used when retiring a duplicate: its tags are the sighting history this
        plugin exists to keep, so they move to the survivor before it is trashed.
        """
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        return [t["tag"] for t in resp.json().get("data", {}).get("tags", [])]

    def post_webpage_item(
        self,
        *,
        url_canonical: str,
        title: str,
        access_date: str,
        tags: list[str],
        item_key: str | None = None,
    ) -> str:
        """Create the item under a key the caller chose.

        The API accepts a client-supplied key matching
        /[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}/. "version": 0 makes this a
        versioned write, which is what lets a duplicate be rejected rather than
        silently creating a second copy — and means no Zotero-Write-Token is
        needed, since the docs call it redundant for versioned requests.
        """
        item_key = item_key or new_zotero_key()
        payload = [
            {
                "key": item_key,
                "version": 0,
                "itemType": "webpage",
                "url": url_canonical,
                "title": title,
                "accessDate": access_date,
                "websiteTitle": _domain(url_canonical),
                "tags": [{"tag": t} for t in tags],
                "collections": [self.collection_key],
            }
        ]
        resp = self._client.post("/items", json=payload)
        if resp.status_code >= 400:
            raise ZoteroError(f"POST /items failed: {resp.status_code} {resp.text}")
        body = resp.json()
        if body.get("failed"):
            raise ZoteroError(f"POST /items had failed entries: {body['failed']}")
        successful = body.get("successful") or {}
        if not successful:
            raise ZoteroError(f"POST /items returned no successful entries: {body}")
        return next(iter(successful.values()))["key"]

    def iter_collection_items(
        self,
        *,
        limit: int = 100,
        attempts: int = 3,
        retry_sleep_s: float = 1.0,
    ) -> Iterator[dict[str, Any]]:
        """Yield every top-level item in the target collection, page by page.

        Streams rather than accumulating: a mature collection runs to thousands of
        items and callers here only ever look at one at a time.

        A timed-out page is retried, because the failure is indistinguishable from
        a short collection to anyone consuming the generator — the sweep just stops
        yielding. Callers run unattended over thousands of items, so one blip must
        not silently truncate the pass. After `attempts` it raises rather than
        returning what it has: a partial sweep reported as a complete one is worse
        than an error.
        """
        start = 0
        while True:
            for attempt in range(1, attempts + 1):
                try:
                    resp = self._client.get(
                        f"/collections/{self.collection_key}/items/top",
                        params={"format": "json", "limit": limit, "start": start},
                    )
                    break
                except httpx.TransportError as e:
                    if attempt == attempts:
                        raise ZoteroError(
                            f"GET collection items at start={start} failed after "
                            f"{attempts} attempts: {e!r}"
                        ) from e
                    if retry_sleep_s:
                        time.sleep(retry_sleep_s * attempt)
            if resp.status_code >= 400:
                raise ZoteroError(
                    f"GET collection items failed: {resp.status_code} {resp.text}"
                )
            page = resp.json()
            yield from page
            if len(page) < limit:
                return
            start += limit

    def query_by_tag(self, tag: str, *, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start = 0
        while True:
            resp = self._client.get(
                "/items",
                params={"tag": tag, "format": "json", "limit": limit, "start": start},
            )
            if resp.status_code >= 400:
                raise ZoteroError(f"GET /items failed: {resp.status_code} {resp.text}")
            page = resp.json()
            items.extend(page)
            if len(page) < limit:
                break
            start += limit
        return items

    def add_tags(
        self,
        item_key: str,
        new_tags: list[str],
        *,
        title_resolver: Callable[[], str] | None = None,
        attempts: int = 3,
    ) -> bool:
        """Idempotent: PATCH only if a tag is missing or an unresolved title got resolved.

        `title_resolver` is a zero-arg callable invoked ONLY when the stored title is
        still the URL-as-fallback sentinel. It reuses the GET this method already
        performs, so re-enrichment costs no extra Zotero round-trip.

        A 412 means another session wrote between our GET and our PATCH — routine
        here, since several Claude sessions capture into one library and the
        prompt hook runs detached. Zotero's documented answer is to refetch and
        reapply, which is what this does: giving up instead lost the tags
        silently, because capture deliberately does not queue failures. Refetching
        also merges the other writer's tags in rather than overwriting them.
        """
        # Resolve at most once for the whole call. The resolver is a live HTTP
        # fetch, so it can succeed on one attempt and fail on the next; re-asking
        # per attempt threw away a title that had already been found and left the
        # item marked unresolved while reporting success. It also let a single
        # contended item spend the run's entire re-enrichment budget.
        memo: dict[str, str] = {}

        def resolve_once() -> str:
            if title_resolver is None:
                return ""
            if "title" not in memo:
                memo["title"] = title_resolver()
            return memo["title"]

        for attempt in range(1, attempts + 1):
            outcome = self._try_add_tags(
                item_key, new_tags, None if title_resolver is None else resolve_once
            )
            if outcome is not None:
                return outcome
            if attempt == attempts:
                raise ZoteroError(
                    f"PATCH /items/{item_key} kept losing to a concurrent write "
                    f"after {attempts} attempts"
                )
        raise AssertionError("unreachable")

    def _try_add_tags(
        self,
        item_key: str,
        new_tags: list[str],
        title_resolver: Callable[[], str] | None,
    ) -> bool | None:
        """One read-modify-write. None means "version moved, try again"."""
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        item = resp.json()
        data = item.get("data", {})
        version = int(resp.headers.get("Last-Modified-Version", item.get("version", 0)))
        existing_tags = {t["tag"] for t in data.get("tags", [])}
        to_add = [t for t in new_tags if t not in existing_tags]
        merged = existing_tags | set(new_tags)

        resolved_title: str | None = None
        if title_resolver is not None and title_is_unresolved(data, existing_tags):
            candidate = title_resolver()
            # The fetcher returns the URL itself when it fails; only a different,
            # non-empty string counts as a real title.
            if candidate and candidate != (data.get("url") or ""):
                resolved_title = candidate
                merged.discard(UNRESOLVED_TITLE_TAG)

        if not to_add and resolved_title is None:
            return False
        patch_body: dict[str, Any] = {"tags": [{"tag": t} for t in sorted(merged)]}
        if resolved_title is not None:
            patch_body["title"] = resolved_title
        resp = self._client.patch(
            f"/items/{item_key}",
            json=patch_body,
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        if resp.status_code == 412:
            return None  # someone else wrote; caller refetches and reapplies
        if resp.status_code not in (200, 204):
            raise ZoteroError(
                f"PATCH /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        return True

    def update_url(self, item_key: str, url: str) -> None:
        """Correct the stored URL of an item.

        Needed because a URL truncated at capture time cannot be repaired by any
        title pass: the repair path reads the item's own URL, so the URL has to
        be fixed first. Guarded by the item version so a concurrent edit is not
        silently overwritten.
        """
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code == 404:
            return  # already gone, idempotent
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        body = resp.json()
        version = resp.headers.get("Last-Modified-Version") or str(
            body.get("version", 0)
        )
        data = body.get("data", {})
        payload: dict[str, Any] = {"url": url}
        # title_is_unresolved detects a failed fetch by title == url. Moving the
        # URL without the title breaks that equality, and the item silently stops
        # looking unresolved — no backfill would ever revisit it again. Carry a
        # sentinel title along; a real title is metadata and stays untouched.
        if (data.get("title") or "").strip() == (data.get("url") or "").strip():
            payload["title"] = url
        resp = self._client.patch(
            f"/items/{item_key}",
            json=payload,
            headers={"If-Unmodified-Since-Version": version},
        )
        if resp.status_code not in (204, 404):
            raise ZoteroError(
                f"PATCH /items/{item_key} (url) failed: {resp.status_code} {resp.text}"
            )

    def trash_item(self, item_key: str) -> None:
        """Move an item to the Zotero trash.

        Deliberately not delete_item: the API's DELETE is permanent, while
        `deleted: 1` leaves the item recoverable from the trash in any Zotero
        client. Anything that removes items in bulk should be undoable.
        """
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code == 404:
            return  # already gone, idempotent
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        version = resp.headers.get("Last-Modified-Version") or str(
            resp.json().get("version", 0)
        )
        resp = self._client.patch(
            f"/items/{item_key}",
            json={"deleted": 1},
            headers={"If-Unmodified-Since-Version": version},
        )
        if resp.status_code not in (204, 404):
            raise ZoteroError(
                f"PATCH /items/{item_key} (trash) failed: "
                f"{resp.status_code} {resp.text}"
            )

    def delete_item(self, item_key: str) -> None:
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code == 404:
            return  # already gone, idempotent
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        item = resp.json()
        version = resp.headers.get("Last-Modified-Version") or str(
            item.get("version", 0)
        )
        resp = self._client.delete(
            f"/items/{item_key}",
            headers={"If-Unmodified-Since-Version": version},
        )
        if resp.status_code not in (204, 404):
            raise ZoteroError(
                f"DELETE /items/{item_key} failed: {resp.status_code} {resp.text}"
            )


def title_is_unresolved(data: dict[str, Any], tags: set[str]) -> bool:
    """True when the stored title is a fallback rather than real metadata."""
    title = (data.get("title") or "").strip()
    return not title or title == (data.get("url") or "") or UNRESOLVED_TITLE_TAG in tags


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
