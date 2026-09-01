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


class ItemGone(ZoteroError):
    """The item this index row points at no longer exists.

    Distinct from a plain ZoteroError on purpose. "The API is down" is
    transient and the right response is to fail and retry later; "a person
    trashed this item" is permanent, and retrying reproduces the same 404 on
    every future citation forever. Only the caller can tell those apart, and it
    could not while both arrived as the same exception.
    """


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
        title_resolver: Callable[[str], str] | None = None,
        attempts: int = 3,
    ) -> bool:
        """Idempotent: PATCH only if a tag is missing or an unresolved title got resolved.

        `title_resolver` is invoked ONLY when the stored title is still the
        URL-as-fallback sentinel, and it is given the item's CURRENT url. It
        reuses the GET this method already performs, so re-enrichment costs no
        extra Zotero round-trip.

        It used to take no argument, so every caller closed it over a url read
        from an earlier snapshot. Backfill could then fetch url A's title, find
        the item had since become url B, write A's title onto B, and clear the
        unresolved marker -- a wrong title, marked resolved so nothing revisits
        it. Optimistic versioning cannot catch that: the version guarding the
        PATCH is fetched after the url changed. Passing the current url removes
        the stale closure rather than guarding it.

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

        def resolve_once(current_url: str) -> str:
            if title_resolver is None:
                return ""
            # Keyed by the url actually asked about: a retry after a 412 refetches
            # the item, and if that changed the url the memo must not answer for
            # the old one.
            if current_url not in memo:
                memo[current_url] = title_resolver(current_url)
            return memo[current_url]

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

    def _open_for_write(
        self,
        item_key: str,
        *,
        expect_url: str | None = None,
        allow_trashed: bool = False,
    ) -> tuple[dict[str, Any], str] | None:
        """Fetch the item a write was selected on, and say whether it may proceed.

        Every writer here needs the same three things before it may touch an
        item: that the item still exists, that it is not in the trash, and that
        it is still the item the caller CHOSE. Each writer used to do its own
        GET and make its own subset of those checks, and the subsets differed:
        the trash rule was implemented in exactly one of four copies. So
        `record_content_hash` would happily stamp provenance onto an item the
        user had thrown away, and the URL write would repair one.

        That is this project's fifth stale-second-copy defect, so the rule is
        stated once here and inherited rather than copied. Adding a fourth
        writer now gets all three checks by construction; there is no list of
        writers to remember to update, which is the failure mode a hand-
        maintained guard cannot catch.

        Two different refusals, because callers must tell them apart:

          * `ItemGone` -- there is nothing to write to, ever. Zotero's trash is
            a FLAG, not a deletion, so a trashed item reads **200 OK with
            `deleted: 1`** and not 404. That is precisely why one writer caught
            it and the others did not: a status-code check cannot see it.
          * `None` -- the item exists but is no longer the one selected. The
            decision was made on a snapshot and optimistic versioning does not
            cover that gap; it stops a write racing the final GET, not an edit
            that landed before it.

        `allow_trashed` is for the removal operations. The rule is "do not write
        provenance onto a removed item", not "never touch one" -- refusing to
        trash something because it is already trashed would make `retire` report
        a failure for work that is already done.
        """
        resp = self._client.get(f"/items/{item_key}")
        if resp.status_code == 404:
            raise ItemGone(f"item {item_key} no longer exists")
        if resp.status_code >= 400:
            raise ZoteroError(
                f"GET /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        body = resp.json()
        data = body.get("data", {})
        if data.get("deleted") and not allow_trashed:
            raise ItemGone(f"item {item_key} is in the trash")
        version = resp.headers.get("Last-Modified-Version") or str(
            body.get("version", 0)
        )
        if (
            expect_url is not None
            and (data.get("url") or "").strip() != expect_url.strip()
        ):
            logger.warning(
                "refusing to write to %s: selected as %r but it is now %r",
                item_key,
                expect_url,
                data.get("url"),
            )
            return None
        return data, version

    def _try_add_tags(
        self,
        item_key: str,
        new_tags: list[str],
        title_resolver: Callable[[str], str] | None,
    ) -> bool | None:
        """One read-modify-write. None means "version moved, try again"."""
        opened = self._open_for_write(item_key)
        if opened is None:  # unreachable: refusal needs an expect_url
            return False
        data, version = opened
        existing_tags = {t["tag"] for t in data.get("tags", [])}
        to_add = [t for t in new_tags if t not in existing_tags]
        merged = existing_tags | set(new_tags)

        resolved_title: str | None = None
        if title_resolver is not None and title_is_unresolved(data, existing_tags):
            if _title_is_real(data):
                # The tag says unresolved; the title says otherwise. A person
                # fixed it by hand and left the tag behind. The tag is a CLAIM
                # about the title, the title is the EVIDENCE, and trusting the
                # claim overwrote real metadata with whatever the fetcher
                # happened to return. Retire the stale claim, keep the evidence.
                merged.discard(UNRESOLVED_TITLE_TAG)
            else:
                candidate = title_resolver(data.get("url") or "")
                # The fetcher returns the URL itself when it fails; only a
                # different, non-empty string counts as a real title.
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

    def _patch_item_url(
        self, item_key: str, url: str, *, expect_url: str | None = None
    ) -> bool:
        """Write an item's URL. PRIVATE -- callers want `url_move.move_url`.

        Needed because a URL truncated at capture time cannot be repaired by any
        title pass: the repair path reads the item's own URL, so the URL has to
        be fixed first. Guarded by the item version so a concurrent edit is not
        silently overwritten.

        This was public, called `update_url`, and that is how 24 index rows came
        to disagree with the library. It was added with no caller anywhere in the
        repo and first used 39 minutes later by a one-off script in a scratch
        directory, which wrote the items and never the index. Nine days later
        `snapshot` read the stale index strings, got 404s, and recorded `gone`:
        a repair manufactured the link rot this tool exists to report truthfully.

        A URL is ONE identity with a copy in each store. Moving it is one
        operation, and `move_url` is that operation -- it cannot be called
        without a db_path, so there is no longer a route that writes half. The
        underscore is not the mechanism; the absence of a public one-store verb
        is. A docstring warning would not have been read by the script that did
        this.
        """
        try:
            opened = self._open_for_write(item_key, expect_url=expect_url)
        except ItemGone:
            # NOT success. Returning None here read as "done" to repair, which
            # then rewrote its SQLite row and counted a rewrite for an item that
            # does not exist — an index entry claiming a corrected URL with
            # nothing behind it.
            return False
        if opened is None:
            return False
        data, version = opened
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
        if resp.status_code == 404:
            return False
        if resp.status_code != 204:
            raise ZoteroError(
                f"PATCH /items/{item_key} (url) failed: {resp.status_code} {resp.text}"
            )
        return True

    EXTRA_HASH_PREFIX = "Content-SHA256:"

    def record_content_hash(
        self, item_key: str, digest: str, *, expect_url: str | None = None
    ) -> bool:
        """Write the content hash onto the item's `extra` field.

        `extra` because the live API says so: `webpage` has no `archive` or
        `archiveLocation` field. That was checked against
        `/itemTypeFields?itemType=webpage` rather than assumed -- the obvious
        guess would have been wrong, and the write would have been silently
        dropped by Zotero.

        Other `extra` lines are preserved and only a previous hash line is
        replaced. `extra` is a field people put their own notes in, and a
        provenance tool that eats them is not one anybody keeps using.
        """
        # Refuses a trashed item, which reads 200 OK with `deleted: 1` and so
        # was invisible to the status-code check this used to do for itself.
        # Stamping provenance onto something the user removed is the same fault
        # `_try_add_tags` had already named and guarded against.
        try:
            opened = self._open_for_write(item_key, expect_url=expect_url)
        except ItemGone:
            return False
        if opened is None:
            return False
        data, version = opened

        kept = [
            line
            for line in (data.get("extra") or "").splitlines()
            if not line.strip().startswith(self.EXTRA_HASH_PREFIX)
        ]
        kept.append(f"{self.EXTRA_HASH_PREFIX} {digest}")
        resp = self._client.patch(
            f"/items/{item_key}",
            json={"extra": "\n".join(kept)},
            headers={"If-Unmodified-Since-Version": version},
        )
        if resp.status_code == 404:
            return False
        if resp.status_code != 204:
            raise ZoteroError(
                f"PATCH /items/{item_key} (extra) failed: "
                f"{resp.status_code} {resp.text}"
            )
        return True

    def trash_item(self, item_key: str, *, expect_url: str | None = None) -> bool:
        """Move an item to the Zotero trash. True if it was trashed.

        Deliberately not delete_item: the API's DELETE is permanent, while
        `deleted: 1` leaves the item recoverable from the trash in any Zotero
        client. Anything that removes items in bulk should be undoable.

        `expect_url` is the URL the caller DECIDED on. Every caller selects from
        a snapshot and destroys later by key, and optimistic versioning does not
        cover that gap: it stops a write racing the final GET, not an edit that
        landed before it. Without this, a sweep could report trashing a font
        asset while it actually trashed the paper the item had become. Given an
        expectation, the item must still be the one that was chosen.

        The annotation used to say `-> None` while two paths returned False, so
        the check was invisible to anyone reading the signature -- and all three
        callers ignored the result.
        """
        # allow_trashed: this IS the removal. Refusing because the item is
        # already in the trash would report a failure for work already done.
        try:
            opened = self._open_for_write(
                item_key, expect_url=expect_url, allow_trashed=True
            )
        except ItemGone:
            # NOT success. Returning None here read as "done" to repair, which
            # then rewrote its SQLite row and counted a rewrite for an item that
            # does not exist — an index entry claiming a corrected URL with
            # nothing behind it.
            return False
        if opened is None:
            return False
        _data, version = opened
        resp = self._client.patch(
            f"/items/{item_key}",
            json={"deleted": 1},
            headers={"If-Unmodified-Since-Version": version},
        )
        if resp.status_code == 404:
            return False
        if resp.status_code not in (204, 404):
            raise ZoteroError(
                f"PATCH /items/{item_key} (trash) failed: "
                f"{resp.status_code} {resp.text}"
            )
        return True

    def delete_item(self, item_key: str) -> bool:
        """Permanently delete an item. True if it was there, False if already gone.

        The signature said `-> None` while returning False on both 404 paths, so
        success and already-gone were both falsy and indistinguishable to any
        caller that checked. Nothing checks today — `zotero_setup` ignores the
        result — which is exactly why it was worth correcting before something
        did: `if not client.delete_item(k)` would have read a successful delete
        as a failure. Round 8 fixed this same shape elsewhere.

        The comment here previously described rewriting a SQLite row and
        counting a repair, which is `update_url`'s story; it had been copied
        wholesale into a method that deletes.
        """
        # allow_trashed for the same reason as trash_item: emptying the trash is
        # the one operation for which "it is already trashed" is not a refusal.
        try:
            opened = self._open_for_write(item_key, allow_trashed=True)
        except ItemGone:
            return False
        if opened is None:  # unreachable: refusal needs an expect_url
            return False
        _data, version = opened
        resp = self._client.delete(
            f"/items/{item_key}",
            headers={"If-Unmodified-Since-Version": version},
        )
        if resp.status_code == 404:
            return False
        if resp.status_code not in (204, 404):
            raise ZoteroError(
                f"DELETE /items/{item_key} failed: {resp.status_code} {resp.text}"
            )
        return True


def _title_is_real(data: dict[str, Any]) -> bool:
    """Whether the stored title is metadata rather than the URL fallback."""
    title = (data.get("title") or "").strip()
    return bool(title) and title != (data.get("url") or "").strip()


def title_is_unresolved(data: dict[str, Any], tags: set[str]) -> bool:
    """True when the stored title is a fallback rather than real metadata."""
    title = (data.get("title") or "").strip()
    return not title or title == (data.get("url") or "") or UNRESOLVED_TITLE_TAG in tags


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
