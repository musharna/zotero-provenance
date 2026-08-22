"""Top-level capture orchestrator — wires extract -> exclude -> dedup -> POST/PATCH."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .sqlite_cache import (
    init_db,
    lookup_url,
    release_url,
    reserve_url,
    set_zotero_key,
    update_last_seen,
)
from .url_processing import (
    NO_CAPTURE_MARKER,
    canonicalize,
    extract_urls,
    is_excluded,
)
from .zotero_client import UNRESOLVED_TITLE_TAG, ZoteroClient, ZoteroError

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT = "general"

# Most title re-fetches to attempt in a single capture run. Each costs one live
# HTTP request inside the Stop hook's few-second budget; unspent retries simply
# happen on a later run, since an unresolved title stays marked until it resolves.
MAX_REENRICH_PER_RUN = 3


@dataclass
class CaptureFailure:
    url: str
    code: str  # "zotero_error" or "unexpected"
    message: str


@dataclass
class CaptureResult:
    urls_seen: int = 0
    urls_new: int = 0
    urls_recurring: int = 0
    urls_excluded: int = 0
    errors: list[CaptureFailure] = field(default_factory=list)


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _is_generated_report(message: str, origin: str) -> bool:
    """True for one of this plugin's own reports, which must not be re-captured.

    Two restrictions, both because the marker is a public string that anyone can
    type. It is honoured only in assistant output — a user prompt is not a
    generated report, and honouring it there let anyone silence capture for a
    whole message by quoting it. And it must LEAD the message, the way
    emit_markdown writes it, so that merely discussing the marker does not
    suppress the citations in the same turn.

    This is the backup layer. The structural rule — a report shows each URL in a
    code span, which extraction reads as displayed rather than cited — is what
    normally does the work.
    """
    return origin == "assistant" and message.lstrip().startswith(NO_CAPTURE_MARKER)


def capture_message(
    *,
    message: str,
    project_slug: str,
    context: str | None,
    today: date,
    db_path: Path,
    zotero: ZoteroClient,
    title_fetcher: Callable[[str], str],
    origin: str = "assistant",
) -> CaptureResult:
    """Process one message: extract URLs, then create or re-tag each in Zotero."""
    init_db(db_path)
    result = CaptureResult()
    if _is_generated_report(message, origin):
        return result
    raw_urls = extract_urls(message)
    canonicals: list[str] = []
    seen_canonicals: set[str] = set()
    for raw in raw_urls:
        c = canonicalize(raw)
        if is_excluded(c):
            result.urls_excluded += 1
            continue
        if c in seen_canonicals:
            continue
        seen_canonicals.add(c)
        canonicals.append(c)
    result.urls_seen = len(canonicals)

    today_iso = today.isoformat()
    seen_tag = f"seen:{today_iso}"
    context_tag = f"context:{context or DEFAULT_CONTEXT}"
    project_tag = f"project:{project_slug}"

    # Re-enrichment spends a live HTTP fetch, so bound it per run: the Stop hook
    # has seconds, and a message can cite many permanently-unfetchable URLs.
    reenrich_budget = [MAX_REENRICH_PER_RUN]

    def make_title_resolver(url: str) -> Callable[[], str]:
        """Resolve this URL's title, but only while the per-run budget lasts.

        Returning the URL means "still unresolved", so an exhausted budget simply
        defers the retry to a later run rather than spending hook time now.
        """

        def resolve() -> str:
            if reenrich_budget[0] <= 0:
                return url
            reenrich_budget[0] -= 1
            return title_fetcher(url)

        return resolve

    # Note: result.errors may include URLs already counted in urls_new/urls_recurring
    # (the Zotero call succeeded but the local SQLite write failed).
    for url in canonicals:
        try:
            existing = lookup_url(db_path, url)
            if existing is None and reserve_url(db_path, url, today):
                # Claimed before the network call, so a second session cannot
                # also decide this URL is new while the POST is in flight and
                # create a duplicate item that dedup could never see again.
                try:
                    title = title_fetcher(url)
                    tags = [
                        context_tag,
                        project_tag,
                        seen_tag,
                        f"domain:{_domain(url)}",
                    ]
                    if title == url:
                        tags.append(UNRESOLVED_TITLE_TAG)
                    key = zotero.post_webpage_item(
                        url_canonical=url,
                        title=title,
                        access_date=today_iso,
                        tags=tags,
                    )
                except BaseException:
                    # No item was created, so the claim must not outlive the
                    # attempt or the URL would be permanently undedupable.
                    release_url(db_path, url)
                    raise
                set_zotero_key(db_path, url, key)
                result.urls_new += 1
                continue

            row = existing if existing is not None else lookup_url(db_path, url)
            key = (row or {}).get("zotero_key") or ""
            if not key:
                # Another session holds the claim and its POST has not landed
                # yet. There is no item to tag, and creating one is the very
                # duplicate the claim exists to prevent, so let the next
                # sighting do it.
                logger.debug("%s is claimed by another session; deferring", url)
                continue
            zotero.add_tags(
                key,
                [seen_tag, context_tag, project_tag],
                title_resolver=make_title_resolver(url),
            )
            result.urls_recurring += 1
            update_last_seen(db_path, url, today)
        except ZoteroError as e:
            logger.error("Zotero API error for %s: %s", url, e)
            result.errors.append(
                CaptureFailure(url=url, code="zotero_error", message=str(e))
            )
        except Exception as e:
            logger.error("Unexpected error for %s: %s", url, e)
            result.errors.append(
                CaptureFailure(url=url, code="unexpected", message=str(e))
            )
    return result
