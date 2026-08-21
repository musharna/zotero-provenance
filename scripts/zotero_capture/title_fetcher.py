"""HTTP GET first ~32KB, parse <title>, 1s budget. URL-as-fallback on any failure."""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 1.0
MAX_BYTES = 32 * 1024

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_USER_AGENT = "zotero-provenance/0.1"

# A DOI is an identifier with an authoritative metadata API, not a web page. The
# publisher HTML behind it is slower (3-4 redirects) and frequently bot-walled,
# so ask the resolver for metadata directly instead of scraping what it lands on.
_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org"})
_CSL_ACCEPT = "application/vnd.citationstyles.csl+json"


def _doi_title(client: httpx.Client, url: str) -> str | None:
    """Ask the DOI resolver for citation metadata. None if it can't be had."""
    try:
        resp = client.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": _CSL_ACCEPT},
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            return None
        title = resp.json().get("title")
    except Exception as e:  # malformed JSON, transport failure, timeout
        logger.debug("DOI content negotiation failed for %s: %s", url, e)
        return None
    # CSL allows a list of title forms; the first is the primary one.
    if isinstance(title, list):
        title = title[0] if title else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def fetch_title(url: str, *, client: httpx.Client | None = None) -> str:
    """Fetch <title> with 1s wall-clock budget. Return URL on any failure.

    Designed for the Stop hook (T8) — URL-as-fallback is the contract;
    callers distinguish "got real title" from "got URL back" by string equality.

    For batch use (≥2 URLs per Stop turn), pass a shared `httpx.Client`
    to amortize TCP/TLS handshake — caller owns lifecycle.
    """
    owns_client = client is None
    try:
        if client is None:
            client = httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True)
        deadline = time.monotonic() + DEFAULT_TIMEOUT_S
        if (urlsplit(url).hostname or "").lower() in _DOI_HOSTS:
            doi_title = _doi_title(client, url)
            if doi_title:
                return doi_title
            # Negotiation is an optimisation, not a new point of failure: fall
            # through and scrape the landing page as before.
        with client.stream("GET", url, headers={"User-Agent": _USER_AGENT}) as resp:
            if resp.status_code >= 400:
                return url
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype.lower():
                return url
            chunks: list[bytes] = []
            received = 0
            for chunk in resp.iter_bytes():
                if time.monotonic() > deadline:
                    return url
                chunks.append(chunk)
                received += len(chunk)
                if received >= MAX_BYTES:
                    break
            body = b"".join(chunks)[:MAX_BYTES]
        try:
            soup = BeautifulSoup(body, "html.parser")
            tag = soup.find("title")
            if tag:
                text = tag.get_text(separator=" ", strip=True)
                if text:
                    return text
        except Exception as e:
            logger.debug("bs4 parse failed for %s: %s", url, e)
            m = _TITLE_RE.search(body.decode("utf-8", errors="replace"))
            if m:
                return m.group(1).strip()
        return url
    except (httpx.HTTPError, OSError) as e:
        logger.debug("title fetch failed for %s: %s", url, e)
        return url
    except Exception as e:
        logger.warning("unexpected title fetch error for %s: %s", url, e)
        return url
    finally:
        if owns_client and client is not None:
            client.close()
