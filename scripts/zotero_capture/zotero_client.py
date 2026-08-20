"""Zotero source capture: hook-driven URL ingestion into a Zotero collection."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

ZOTERO_API_BASE = "https://api.zotero.org"


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
            base_url=f"{ZOTERO_API_BASE}/{library_type}s/{library_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Zotero-API-Version": "3",
                "User-Agent": "zotero-provenance/0.1",
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

    def post_webpage_item(
        self,
        *,
        url_canonical: str,
        title: str,
        access_date: str,
        tags: list[str],
    ) -> str:
        payload = [
            {
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

    def add_tags(self, item_key: str, new_tags: list[str]) -> bool:
        """Idempotent: PATCH only if at least one tag is missing. Returns True if PATCH happened."""
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        item = resp.json()
        version = int(resp.headers.get("Last-Modified-Version", item.get("version", 0)))
        existing_tags = {t["tag"] for t in item["data"].get("tags", [])}
        to_add = [t for t in new_tags if t not in existing_tags]
        if not to_add:
            return False
        merged = existing_tags | set(new_tags)
        patch_body = {"tags": [{"tag": t} for t in sorted(merged)]}
        resp = self._client.patch(
            f"/items/{item_key}",
            json=patch_body,
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        if resp.status_code not in (200, 204):
            raise ZoteroError(
                f"PATCH /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        return True

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


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
