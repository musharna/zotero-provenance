"""Top-level capture orchestrator — wires extract -> exclude -> dedup -> POST/PATCH."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .sqlite_cache import init_db, insert_url, lookup_url, update_last_seen
from .url_processing import canonicalize, extract_urls, is_excluded
from .zotero_client import ZoteroClient, ZoteroError

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT = "general"


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


def capture_message(
    *,
    message: str,
    project_slug: str,
    context: str | None,
    today: date,
    db_path: Path,
    zotero: ZoteroClient,
    title_fetcher: Callable[[str], str],
) -> CaptureResult:
    """Process one message: extract URLs, then create or re-tag each in Zotero."""
    init_db(db_path)
    result = CaptureResult()
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

    # Note: result.errors may include URLs already counted in urls_new/urls_recurring
    # (the Zotero call succeeded but the local SQLite write failed).
    for url in canonicals:
        try:
            existing = lookup_url(db_path, url)
            if existing is None:
                title = title_fetcher(url)
                tags = [context_tag, project_tag, seen_tag, f"domain:{_domain(url)}"]
                key = zotero.post_webpage_item(
                    url_canonical=url,
                    title=title,
                    access_date=today_iso,
                    tags=tags,
                )
                result.urls_new += 1
                insert_url(db_path, url, key, today)
            else:
                key = existing["zotero_key"]
                zotero.add_tags(key, [seen_tag, context_tag, project_tag])
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
